# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Salesforce sub-agent.

Searches an organization's Salesforce document library (Salesforce Files) via the
REST/SOSL API using an app-only (2LO / client-credentials) flow. Salesforce
full-text-indexes uploaded documents; the client downloads the top hits and
extracts a text excerpt. Deployed to Cloud Run and reached by
the root Code Assist agent over A2A (the A2A surface is wired automatically by the
scaffold in `app/fast_api_app.py` + `app/app_utils/a2a.py`).
"""

import os
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types
from app.salesforce_client import SalesforceClient
from app.tools import build_search_salesforce

load_dotenv()

MODEL = os.environ["MODEL"]
# My Domain host (used to build the REST base URL). The Connected App consumer
# key/secret are gone: Auth Manager holds them in the 2LO auth provider and resolves
# the token at tool-call time using the agent's own identity.
SALESFORCE_DOMAIN = os.environ["SALESFORCE_DOMAIN"]

salesforce_client = SalesforceClient(
    domain=SALESFORCE_DOMAIN,
    api_version=os.environ.get("SALESFORCE_API_VERSION"),
)
search_salesforce = build_search_salesforce(salesforce_client)


async def generate_memories_callback(callback_context: CallbackContext) -> None:
    """Persist long-term memories from the current session (background generation)."""
    await callback_context.add_session_to_memory()


root_agent = Agent(
    name="salesforce_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Specialized assistant for Salesforce: searches the organization's "
        "document library (Salesforce Files) via the Salesforce REST/SOSL API."
    ),
    instruction=(
        "You are the Salesforce Specialist Agent. "
        "To answer any question you MUST call the `search_salesforce` tool, "
        "passing the user's clean question as the query; never answer from your own "
        "knowledge. "
        "The search spans every document (file) the organization has uploaded, "
        "so you do not need a file name or folder from the user - just pass "
        "their query to `search_salesforce`. "
        "After the tool returns, answer with what you found: be concise, cite "
        "the document names / URLs you used, and quote the returned snippets "
        "when they help answer the question. "
        "If the tool returns no results, say so plainly. "
        "If you recall relevant details about the user from earlier "
        "conversations, use them to tailor your search and answer."
    ),
    tools=[search_salesforce, PreloadMemoryTool()],
    after_agent_callback=generate_memories_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)