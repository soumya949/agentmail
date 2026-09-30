"""Governed AgentMail tools for the OpenAI Agents SDK."""

from openbox_agentmail import create_openbox_mail_agent, governed_tools

agent = create_openbox_mail_agent(surface="openai-toolkit")

tools = governed_tools("openai", agent, names=[
    "send_message", "reply_to_message", "get_thread", "list_threads",
])

# from agents import Agent
# bot = Agent(name="Email Agent", instructions="...", tools=tools)
