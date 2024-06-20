from __future__ import annotations

import json
import os
import shutil
import dotenv

from typing import Type, Optional
from typing_extensions import Self

from rich import print

from agents.agent import Agent
from agents.state_management import ConversationStateMachine
from agents.memory import Memory
from agents.tot.tot import ToT

from utils.parsing import dict2xml, xml2xmlstr, xmlstr2dict
from utils.llm import llm_turn

from anthropic import Anthropic


class AgentManager():
    PRINT_PREFIX = "[bold][AgentMgr][/bold]"

    _instance = None

    def __new__(cls: Type[Self], *args, **kwargs) -> Self:
        if "client" in kwargs:
            cls.client: Anthropic = kwargs['client']

        if "prefix" in kwargs:
            cls.PRINT_PREFIX = f"{kwargs['prefix']} {cls.PRINT_PREFIX}"

        if cls._instance is None:
            print(f"{cls.PRINT_PREFIX} Creating a singleton AgentManager")
            cls._instance = super(AgentManager, cls).__new__(cls)
            cls._instance.__initialized = False

        return cls._instance
    
    def __init__(self, client: Optional[Anthropic] = None, prefix: str = "") -> None:
        if not self.__initialized:
            dotenv.load_dotenv()
            
            sessions_dir = os.environ.get("SESSIONS_DIR")
            if sessions_dir is not None:
                if os.path.exists(sessions_dir):
                    shutil.rmtree(sessions_dir)
            else:
                print(f"[red][bold]{self.PRINT_PREFIX} SESSIONS_DIR environment variable not set (check .env)[/bold][/red]")
                exit(1)

            self.agents: list[Agent] = []

            agtmgr_dir, input_dir = os.environ.get("AGTMGR_DIR"), os.environ.get("INPUT_DIR")
            if agtmgr_dir is None:
                print(f"[red][bold]{self.PRINT_PREFIX} AGTMGR_DIR environment variable not set (check .env)[/bold][/red]")
                exit(1)
            if input_dir is None:
                print(f"[red][bold]{self.PRINT_PREFIX} INPUT_DIR environment variable not set (check .env)[/bold][/red]")
                exit(1)

            with open(os.path.join(agtmgr_dir, input_dir, "states.json")) as file:
                state_data = json.load(file)
                print(f"{self.PRINT_PREFIX} loaded state_data")

            with open(os.path.join(agtmgr_dir, input_dir, "transitions.json")) as file:
                transition_data = json.load(file)
                print(f"{self.PRINT_PREFIX} loaded transition_data")

            self.csm = ConversationStateMachine(state_data=state_data, transition_data=transition_data, init_state_path="AwaitIPC", prefix=self.PRINT_PREFIX, owner_class_name="AgentManager")
            self.memory = Memory(environ_path_key="AGTMGR_DIR", prefix=self.PRINT_PREFIX)

            self.parsed_response = None

            self.__initialized = True
            print(f"{self.PRINT_PREFIX} Initialized the instance")

    def ipc(self, trigger: str, data: dict) -> None:
        self.csm.transition(trigger, locals())

        while self.csm.current_state.get_hpath() != "AwaitIPC":

            match self.csm.current_state.get_hpath():

                case "RouteAction":
                    action = data['action']

                    print(f"{self.PRINT_PREFIX} Routing action: {action}")

                    print(self.agents)

                    agents_xmlstr = self.get_agents_xmlstr()
                    print(f"{self.PRINT_PREFIX} agents_str:\n{agents_xmlstr}")

                    action_xml = dict2xml(action)
                    print(f"{self.PRINT_PREFIX} action_xml:\n{action_xml}")

                    action_xmlstr = xml2xmlstr(action_xml)
                    print(f"{self.PRINT_PREFIX} action_xmlstr:\n{action_xmlstr}")

                    self.memory.prime_all_prompts(self.csm.current_state.get_hpath(), "AGTMGR_DIR", dynamic_metaprompt=None, user_frmt={"agents_str": agents_xmlstr, "task": action_xmlstr})
                    print(f"{self.PRINT_PREFIX} self.memory.conversation_history:\n{self.memory.conversation_history}")

                    text = llm_turn(client=self.client,
                                    prompts={'system': self.memory.get_system_prompt(),
                                            'messages': self.memory.get_messages()},
                                    stop_sequences=["</output>"],
                                    temperature=0.0)
                    
                    self.memory.store_llm_response("<output>" + text + "</output>")

                    agent_selection = xmlstr2dict(text, self.client)
                    print(f"{self.PRINT_PREFIX} agent_selection:\n{agent_selection}")

                    if not agent_selection['name']:
                        self.csm.transition("CreateAgent", locals())
                    else:
                        self.csm.transition("AssignAgent", locals())

                case "CreateAgent":
                    self.memory.prime_all_prompts(self.csm.current_state.get_hpath(), "AGTMGR_DIR", dynamic_metaprompt=None, system_frmt={"task": action_xmlstr}, user_frmt={"task": action_xmlstr})

                    print(f"{self.PRINT_PREFIX} self.memory.conversation_history:\n{self.memory.conversation_history}")

                    text = llm_turn(client=self.client,
                                    prompts={'system': self.memory.get_system_prompt(),
                                             'messages': self.memory.get_messages()},
                                    stop_sequences=["</output>"],
                                    temperature=0.0)
                    
                    self.memory.store_llm_response("<output>" + text + "</output>")

                    new_agent_info = xmlstr2dict(text, self.client)
                    print(f"{self.PRINT_PREFIX} new_agent_info:\n{new_agent_info}")

                    print(f"{self.PRINT_PREFIX} Creating agent: {new_agent_info['name']}")

                    new_agent = ToT(client=self.client,
                                    name=new_agent_info['name'],
                                    description=new_agent_info['description'],
                                    tasks=[action])
                    
                    self.register_agent(new_agent)

                    new_agent.run()
                    
                    self.csm.transition("AwaitIPC", locals())
                    
                case "AssignAgent":
                    for agent in self.agents:
                        if agent.name == agent_selection['name']:
                            agent.add_task(action)
                            agent.run()
                            self.csm.transition("AwaitIPC", locals)
                            break

    def register_agent(self, agent: Agent) -> None:
        print(f"{self.PRINT_PREFIX} Registering agent: {agent.name}")
        self.agents.append(agent)

    def get_agents_xmlstr(self) -> str:
        agents_xmlstr: str = ""

        for i, agent in enumerate(self.agents):
            agents_xmlstr += f"<agent idx={i}>\n"
            agents_xmlstr += f"<name>{agent.name}</name>\n"
            agents_xmlstr += f"<description>{agent.description}</description>\n"
            agents_xmlstr += f"<tasks>\n"

            for task in agent.tasks:
                if not isinstance(task, dict):
                    print(f"[bold][red]{self.PRINT_PREFIX} task was {type(task)}, expected dict[/red][/bold]")
                    exit(1)
                    
                task_xml = dict2xml(task)
                task_str = xml2xmlstr(task_xml)

                agents_xmlstr += task_str + "\n"

            agents_xmlstr += f"</tasks>\n"
            agents_xmlstr += f"</agent>\n"

        return agents_xmlstr



