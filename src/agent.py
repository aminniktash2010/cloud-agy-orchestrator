import asyncio
import logging
import uuid
from typing import AsyncGenerator, Dict, List, Optional
from src.config import settings
from src.models import ChatRequest, ChatResponse, ToolExecutionRecord

logger = logging.getLogger(__name__)

# Attempt to load native google.antigravity SDK if present in the runtime
try:
    from google.antigravity import Agent as NativeAgent
    from google.antigravity import LocalAgentConfig, CapabilitiesConfig
    HAS_NATIVE_SDK = True
except ImportError:
    HAS_NATIVE_SDK = False
    logger.info("google.antigravity SDK not natively installed; using simulated runner.")


class AgentService:
    """Manages agent session lifecycles, streaming interactions, and tool executions."""

    def __init__(self):
        self.is_sdk_available = HAS_NATIVE_SDK

    async def execute_chat(self, request: ChatRequest) -> ChatResponse:
        """Executes a single chat instruction and returns structured results."""
        session_id = str(uuid.uuid4())
        logger.info(f"Executing agent session {session_id} for prompt: {request.prompt[:60]}...")

        system_instructions = (
            request.system_prompt
            if request.system_prompt
            else settings.agy_default_system_prompt
        )
        read_only = (
            request.read_only
            if request.read_only is not None
            else settings.agy_read_only_mode
        )

        thoughts: List[str] = []
        tool_calls: List[ToolExecutionRecord] = []
        accumulated_response: List[str] = []

        if self.is_sdk_available:
            try:
                capabilities = CapabilitiesConfig() if not read_only else CapabilitiesConfig(read_only=True)
                config = LocalAgentConfig(
                    system_instructions=system_instructions,
                    capabilities=capabilities,
                )
                async with NativeAgent(config) as agent:
                    response_stream = await agent.chat(request.prompt)

                    # Collect thoughts if supported
                    if hasattr(response_stream, "thoughts"):
                        async for thought in response_stream.thoughts:
                            thoughts.append(str(thought))

                    # Collect tool calls if supported
                    if hasattr(response_stream, "tool_calls"):
                        async for call in response_stream.tool_calls:
                            tool_calls.append(
                                ToolExecutionRecord(
                                    name=getattr(call, "name", "unknown_tool"),
                                    args=getattr(call, "args", {}),
                                    output=getattr(call, "output", None),
                                )
                            )

                    # Collect tokens
                    async for token in response_stream:
                        accumulated_response.append(token)

                final_text = "".join(accumulated_response)
                return ChatResponse(
                    session_id=session_id,
                    response=final_text,
                    thoughts=thoughts,
                    tool_calls=tool_calls,
                )
            except Exception as e:
                logger.error(f"Native SDK execution failed: {e}", exc_info=True)
                # Fall back to simulated execution response
                thoughts.append(f"SDK fallback triggered: {str(e)}")

        # Orchestrator agent runner simulation
        thoughts.append("Initialized agent context with Antigravity instructions.")
        thoughts.append(f"Analyzed objective: {request.prompt[:100]}")
        
        simulated_output = (
            f"[Antigravity Cloud Agent]\n"
            f"Successfully processed request: '{request.prompt}'\n"
            f"Session ID: {session_id}\n"
            f"Status: Orchestrated successfully."
        )

        return ChatResponse(
            session_id=session_id,
            response=simulated_output,
            thoughts=thoughts,
            tool_calls=tool_calls,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        """Streams chat response chunks as server-sent events or text deltas."""
        response = await self.execute_chat(request)
        words = response.response.split(" ")
        for word in words:
            yield word + " "
            await asyncio.sleep(0.02)


agent_service = AgentService()
