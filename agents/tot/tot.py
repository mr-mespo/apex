import os
import json

from copy import deepcopy
from typing import Any, Optional

from rich import print

from agents.state_management import ConversationStateMachine
from agents.execution_management import CodeExecutor

from agents.memory import Memory

from utils.parsing import xmlstr2dict, strip_step_tags, extract_language_and_code

from anthropic import Anthropic


PLAN_COUNT_HL = int(os.environ.get("PLAN_COUNT_HL"))
PLAN_COUNT = int(os.environ.get("PLAN_COUNT"))

VOTER_COUNT = int(os.environ.get("VOTER_COUNT"))
PROPOSER_COUNT = int(os.environ.get("PROPOSER_COUNT"))

EVAL_CATEGORIES = ["correctness", "elegance", "understandability", "overall"]

TEMP = 0.7


class ToT():
    PRINT_PREFIX = "[blue][bold][ToT][/bold][/blue]"

    def __init__(self, name, description, tasks, client: Anthropic):
        self.name = name
        self.description = description
        self.tasks = tasks
        self.client = client

        with open(os.path.join(os.environ.get("TOT_DIR"), os.environ.get("INPUT_DIR"), "states.json")) as file:
            state_data = json.load(file)
            print(f"{self.PRINT_PREFIX} loaded state_data")

        with open(os.path.join(os.environ.get("TOT_DIR"), os.environ.get("INPUT_DIR"), "transitions.json")) as file:
            transition_data = json.load(file)
            print(f"{self.PRINT_PREFIX} loaded transition_data")

        self.csm = ConversationStateMachine(state_data=state_data, transition_data=transition_data, init_state_path='Plan', prefix=self.PRINT_PREFIX, owner_class_name="ToT")

        self.memory = Memory(prefix=self.PRINT_PREFIX)

        self.code_executor = CodeExecutor(self.PRINT_PREFIX)

        self.run()
    
    def llm_turn(self, prompts, temperature, stop_sequences) -> str:
        llm_response = self.csm.current_state.llm_call(client=self.client,
                                        formatted_system=prompts["system"],
                                        formatted_messages=prompts["messages"],
                                        stop_sequences=stop_sequences,
                                        temperature=temperature)
        
        print(f"{self.PRINT_PREFIX} llm_response: {llm_response}")
        return llm_response.content[0].text
    
    def run(self):

        step_num = 1

        plan_memories = self.init_plan_memories()

        plan_voter_memories = self.init_voter_memories()
        propose_voter_memories = self.init_voter_memories()
        exec_voter_memories = self.init_voter_memories()

        exec_history = ""

        while self.csm.current_state.name != "Done":

            start_seq = f"<step_{step_num}>"
            stop_seq = f"</step_{step_num}>"

            match self.csm.current_state.get_hpath():
                
                case "Plan":

                    for plan_memory in plan_memories:
                        self.plan(step_num, start_seq, stop_seq, plan_memory)

                    self.csm.transition("PlanVote", locals())

                case "PlanVote":
                    for plan_memory in plan_memories:
                        for voter in plan_voter_memories:
                            step_plan = plan_memory.conversation_history[-1]["content"]

                            addl_user_frmt = {"step_plan": step_plan}
                            addl_sys_frmt = {"exec_history": exec_history}
                            
                            self.vote(step_num, start_seq, stop_seq, plan_memory, voter, addl_sys_frmt=addl_sys_frmt, addl_user_frmt=addl_user_frmt)

                    self.csm.transition("SumPlanVotes", locals())

                case "SumPlanVotes":
                    sum_scores, avg_scores = self.reduce_scores(plan_voter_memories, step_num)
                    self.csm.transition("ChoosePlan", locals())

                case "ChoosePlan":
                    best_plan_idx = self.choose_plan(avg_scores)
                    plan_memories = self.copyover_from_best_plan(plan_memories, best_plan_idx)

                    self.csm.transition("Propose", locals())
            
                case "Propose":
                    for plan_memory in plan_memories:
                        self.plan(step_num, start_seq, stop_seq, plan_memory)
                    
                    self.csm.transition("ProposeVote", locals())

                case "ProposeVote":
                    for plan_memory in plan_memories:
                        for voter in propose_voter_memories:
                            step_plan = strip_step_tags(plan_memory.conversation_history[-3]["content"])
                            step_implementation = strip_step_tags(plan_memory.conversation_history[-1]["content"]).strip()

                            addl_user_frmt = {"step_plan": step_plan, "step_implementation": step_implementation}
                            self.vote(step_num, start_seq, stop_seq, plan_memory, voter, addl_user_frmt=addl_user_frmt)

                    self.csm.transition("SumProposeVotes", locals())

                case "SumProposeVotes":
                    sum_scores, avg_scores = self.reduce_scores(propose_voter_memories, step_num)
                    self.csm.transition("ChooseProposition", locals())

                case "ChooseProposition":
                    best_plan_idx = self.choose_plan(avg_scores)
                    plan_memories = self.copyover_from_best_plan(plan_memories, best_plan_idx)

                    self.csm.transition("Exec", locals())

                case "Exec":
                    best_plan_memory = self.get_plan_memory(best_plan_idx, plan_memories)

                    best_plan = best_plan_memory.conversation_history[-3]["content"]
                    best_plan_fenced_code = best_plan_memory.conversation_history[-1]["content"]
                    
                    parsed_code = extract_language_and_code(best_plan_fenced_code)
                    if not parsed_code:
                        print(f"[red][bold]{self.PRINT_PREFIX} code not parsable:\n{best_plan_fenced_code}[/bold][/red]")
                        exit(1)
                    
                    language, code = parsed_code

                    print(f"{self.PRINT_PREFIX} executing code:\n{code}", end='')
                    output, error = self.code_executor.execute_code(code)

                    exec_history += start_seq + "\n"
                    exec_history += "<plan>" + strip_step_tags(best_plan) + "</plan>" + "\n"
                    exec_history += "<code>" + code + "</code>" + "\n"
                    exec_history += "<std_output>" + output + "</std_output>" + "\n"
                    exec_history += "<std_error>" + error + "</std_error>" + "\n"
                    exec_history += stop_seq + "\n"

                    print(f"{self.PRINT_PREFIX} output:\n{output}", end='')
                    print(f"{self.PRINT_PREFIX} error:\n{error}", end='')

                    if error:
                        self.csm.transition("PlanErrorFix", locals())
                    else:
                        self.csm.transition("ExecVote", locals())

                case "PlanErrorFix":
                    for plan_memory in plan_memories:
                        addl_frmt = {"output": output, "error": error}
                        self.plan(step_num, start_seq, stop_seq, plan_memory, addl_sys_frmt=addl_frmt, addl_user_frmt=addl_frmt)

                    step_num += 1
                    self.csm.transition("PlanVote", locals())

                case "ExecVote":
                    for voter in exec_voter_memories:
                        addl_frmt = {"output": output,
                                     "error": error,
                                     "step_plan": step_plan,
                                     "step_implementation": step_implementation}
                        
                        self.vote(step_num, "<output>", "</output>", best_plan_memory, voter, addl_user_frmt=addl_frmt)

                    self.csm.transition("SumExecVote", locals())

                case "SumExecVote":
                    sum_yes_votes, avg_yes_votes = self.reduce_scores_exec(exec_voter_memories, step_num)

                    if avg_yes_votes > 0.5:
                        self.csm.transition("Done", locals())
                    else:
                        step_num += 1
                        self.csm.transition("Plan", locals())

        print(f"{self.PRINT_PREFIX} done!")
        return


    def init_plan_memories(self) -> list[Memory]:
        plan_memories = []

        for plan_idx in range(PLAN_COUNT):
            plan_memory: Memory = Memory(prefix=f"{self.PRINT_PREFIX} (plan {plan_idx})")
            plan_memory.plan_idx = plan_idx
            plan_memories.append(plan_memory)

        return plan_memories
    
    def init_voter_memories(self) -> list[Memory]:
        voter_memories = []
        
        for voter_idx in range(VOTER_COUNT):
            voter_memory: Memory = Memory(prefix=f"{self.PRINT_PREFIX} (voter {voter_idx})")

            voter_idx: int = voter_idx
            voter_memory.voter_idx = voter_idx

            vote_results: list[dict[str, Any]] = []
            voter_memory.vote_results = vote_results

            voter_memories.append(voter_memory)

        return voter_memories
    
    def get_plan_memory(self, plan_idx: int, plan_memories: list[Memory]) -> Memory:
        for plan_memory in plan_memories:
            if plan_memory.plan_idx == plan_idx:
                return plan_memory
    
    def plan(self, step_num: int, START_SEQ: str, STOP_SEQ: str, plan_memory: Memory, addl_sys_frmt: dict = {}, addl_user_frmt: dict = {}) -> None:
        system_frmt = {"task": self.tasks[-1]}
        system_frmt.update(addl_sys_frmt)

        system = plan_memory.load_sys_prompt(self.csm.current_state.get_hpath(), "TOT_DIR", frmt=system_frmt)

        user_frmt = {"step_num": step_num}
        user_frmt.update(addl_user_frmt)

        plan_memory.load_user_prompt(self.csm.current_state.get_hpath(), "TOT_DIR", dynamic_metaprompt=None, frmt=user_frmt)
        plan_memory.load_assistant_prefill(START_SEQ)

        llm_response = self.llm_turn(prompts={"system": system, "messages": plan_memory.conversation_history}, temperature=TEMP, stop_sequences=[STOP_SEQ])

        plan_memory.store_llm_response(START_SEQ + llm_response + STOP_SEQ)

    def vote(self, step_num: int, START_SEQ: str, STOP_SEQ: str, plan_memory: Memory, voter: Memory, addl_sys_frmt: dict = {}, addl_user_frmt: dict = {}) -> None:
        system_frmt = {"task": self.tasks[-1]}
        system_frmt.update(addl_sys_frmt)

        system = voter.load_sys_prompt(self.csm.current_state.get_hpath(), "TOT_DIR", frmt=system_frmt)

        user_frmt = {"step_num": step_num}
        user_frmt.update(addl_user_frmt)

        voter.load_user_prompt(self.csm.current_state.get_hpath(), "TOT_DIR", dynamic_metaprompt=None, frmt=user_frmt)
        voter.load_assistant_prefill(START_SEQ)

        print(f"{self.PRINT_PREFIX} voter {voter.voter_idx}, plan {plan_memory.plan_idx}:")
        llm_response = self.llm_turn(prompts={"system": system, "messages": voter.conversation_history}, temperature=TEMP, stop_sequences=[STOP_SEQ])

        xml_text = START_SEQ + llm_response + STOP_SEQ
        voter.store_llm_response(xml_text)

        vote_result = {"step_num": step_num, "plan_idx": plan_memory.plan_idx, "xml_text": xml_text}
        voter.vote_results.append(vote_result)

    def reduce_scores(self, voter_memories: Memory, step_num: int):
        sum_scores = {plan_idx: {eval_category: 0 for eval_category in EVAL_CATEGORIES} for plan_idx in range(PLAN_COUNT)}
        avg_scores = deepcopy(sum_scores)

        scores: dict[int, list] = {}

        for voter in voter_memories:
            for vote_result in voter.vote_results:
                if vote_result['step_num'] == step_num:
                    if not vote_result['plan_idx'] in scores:
                        scores[vote_result['plan_idx']] = []

                    parsed_scores = xmlstr2dict(vote_result['xml_text'])
                    scores[vote_result['plan_idx']].append(parsed_scores)

                    for category in EVAL_CATEGORIES:
                        sum_scores[vote_result['plan_idx']][category] += int(parsed_scores[f'step_{step_num}'][category]['score'])

        for plan_idx, scores in sum_scores.items():
            for category, score in scores.items():
                avg_scores[plan_idx][category] = score / len(voter_memories)
        
        print(f"{self.PRINT_PREFIX} sum_scores:\n{sum_scores}")
        print(f"{self.PRINT_PREFIX} avg_scores:\n{avg_scores}")
        
        return sum_scores, avg_scores
    
    def reduce_scores_exec(self, voter_memories: Memory, step_num: int):
        sum_yes_votes = 0
        avg_yes_votes = 0

        for voter in voter_memories:
            for vote_result in voter.vote_results:
                if vote_result['step_num'] == step_num:
                    parsed_scores = xmlstr2dict(vote_result['xml_text'])

                    if parsed_scores['output']['complete'] == "yes":
                        sum_yes_votes += 1

        avg_yes_votes = sum_yes_votes / len(voter_memories)
        
        print(f"{self.PRINT_PREFIX} sum_yes_votes: {sum_yes_votes}")
        print(f"{self.PRINT_PREFIX} avg_yes_votes: {avg_yes_votes}")
        
        return sum_yes_votes, avg_yes_votes

    def choose_plan(self, avg_scores):
        avg_scores_list: list[str, float | int] = [{**avg_scores[i], "plan_idx": i} for i in sorted(avg_scores.keys())]    
        avg_scores_list_sorted: list[str, float | int] = sorted(avg_scores_list,
                                                                  key=lambda x: tuple( (x[category] for category in EVAL_CATEGORIES if not (category == "plan_idx") ) ),
                                                                  reverse=True)

        best_plan_scores = avg_scores_list_sorted[0]

        print(f"{self.PRINT_PREFIX} sum_scores_list_sorted:\n{avg_scores_list_sorted}")
        print(f"{self.PRINT_PREFIX} best_plan_scores:\n{best_plan_scores}")

        best_plan_idx = avg_scores_list_sorted[0]['plan_idx']
        print(f"{self.PRINT_PREFIX} plan {best_plan_idx} is the best plan")

        return best_plan_idx

    def copyover_from_best_plan(self, plan_memories, best_plan_idx) -> list[Memory]:
        best_plan = None
        for plan_memory in plan_memories:
            if plan_memory.plan_idx == best_plan_idx:
                best_plan = plan_memory
                break

        for plan_memory in plan_memories:
            if plan_memory.plan_idx != best_plan_idx:
                plan_memory.conversation_history = deepcopy(best_plan.conversation_history)

        return plan_memories
