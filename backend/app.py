"""
Direct Agent Communication AgentCore Application
Clean entry point for AgentCore runtime deployment
"""

from bedrock_agentcore import BedrockAgentCoreApp
from agents import (
    create_supervisor_agent,
    create_patient_context_agent,
    create_global_guidelines_agent,
    create_local_regulatory_agent
)
from agents.supervisor_agent import register_agent
from utils.session_manager import (
    create_session,
    get_session_context,
    update_session_activity,
    check_session_timeout
)
from utils.agui_emitter import AGUIEventEmitter
import logging
import json
import os
import uuid

# Enable observability for custom clinical spans
os.environ['AGENT_OBSERVABILITY_ENABLED'] = 'true'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Print observability configuration on startup
try:
    from utils.clinical_observability import print_otel_config
    logger.info("=" * 80)
    logger.info("OBSERVABILITY CONFIGURATION")
    logger.info("=" * 80)
    print_otel_config()
    logger.info("=" * 80)
except Exception as e:
    logger.warning(f"Could not print observability config: {e}")

# Initialize AgentCore app
app = BedrockAgentCoreApp()

# Initialize and register all agents
def initialize_agent_system():
    """Initialize the direct agent communication system with ClinicalCompass agents"""
    
    # Create ClinicalCompass agents
    supervisor_agent = create_supervisor_agent()
    patient_context_agent = create_patient_context_agent()
    global_guidelines_agent = create_global_guidelines_agent()
    local_regulatory_agent = create_local_regulatory_agent()
    
    # Register all agents for inter-agent communication
    register_agent("supervisor_agent", supervisor_agent)
    register_agent("patient_context_agent", patient_context_agent)
    register_agent("global_guidelines_agent", global_guidelines_agent)
    register_agent("local_regulatory_agent", local_regulatory_agent)
    
    logger.info("Direct agent communication system initialized")
    logger.info("ClinicalCompass agents: supervisor_agent, patient_context_agent, global_guidelines_agent, local_regulatory_agent")
    
    return supervisor_agent

# Initialize the system
supervisor_agent = initialize_agent_system()

@app.entrypoint
async def agent_invocation(payload):
    """AgentCore entry point with streaming - routes to Supervisor Agent for ClinicalCompass workflow with session management"""
    try:
        # Parse payload if it's a string
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse payload as JSON: {payload[:200]}")
                yield {"error": "Invalid JSON payload"}
                return
        elif isinstance(payload, bytes):
            try:
                payload = json.loads(payload.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.error(f"Failed to parse bytes payload")
                yield {"error": "Invalid payload encoding"}
                return

        logger.info(f"Received payload: {payload}")
        logger.info(f"Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'Not a dict'}")
        
        # Handle different payload formats
        cds_key = None
        cds_env = None
        if "Prompt" in payload:
            # Frontend format: {"ReferralId": null, "Prompt": "...", "LanguageCode": "en-GB", "SessionId": "..."}
            user_message = payload.get("Prompt", "")
            referral_id = payload.get("ReferralId")
            session_id = payload.get("SessionId")
            language_code = payload.get("LanguageCode", "en-GB")
            
            # Extract cds-key and cds-env from multiple possible locations
            cds_key = (payload.get("cds_key") or 
                      payload.get("cds-key") or 
                      payload.get("headers", {}).get("cds-key") or
                      payload.get("headers", {}).get("cds_key"))
            
            cds_env = (payload.get("cds_env") or 
                      payload.get("cds-env") or 
                      payload.get("headers", {}).get("cds-env") or
                      payload.get("headers", {}).get("cds_env"))
            
            logger.info(f"Frontend format - ReferralId: {referral_id}, SessionId: {session_id}, Language: {language_code}")
            logger.info(f"CDS-Key found: {'***' + cds_key[-4:] if cds_key else 'None'}")
            logger.info(f"CDS-Env found: {cds_env or 'None'}")
        else:
            # Test/Lambda format: {"prompt": "...", "message": "...", "query": "..."}
            user_message = payload.get("prompt", payload.get("message", payload.get("query", "")))
            referral_id = payload.get("patient_id")
            session_id = payload.get("session_id")
        
        # NOTE: Request-scoped values (cds_key, cds_env, referral_id) are kept as
        # local variables and passed explicitly via the per-invocation payload
        # below. They are deliberately NOT written to os.environ: the AgentCore
        # container is long-lived and shared across concurrent invocations, so
        # process-global env vars would leak one request's patient/referral
        # context into another (cross-request PHI contamination).
        if not cds_key:
            logger.warning("No CDS key found, using default auth")

        if not user_message:
            yield {"error": "No message or Prompt provided"}
            return
        
        # === SESSION MANAGEMENT ===
        # Patient ID is the actor_id for session management
        patient_id = referral_id or "unknown"
        session_context = {}
        
        # Check for existing session
        if session_id:
            logger.info(f"Checking existing session: {session_id}")
            
            # Check timeout
            timeout_status = check_session_timeout(patient_id, session_id)
            
            if timeout_status.get('status') == 'not_found':
                logger.warning(
                    f"Session {session_id} not found for patient {patient_id}; "
                    "creating a new session with the requested session ID"
                )
                session_id = create_session(
                    patient_id,
                    user_message,
                    session_id=session_id,
                )
                logger.info(f"Created replacement session: {session_id}")
            elif timeout_status['timed_out']:
                logger.warning(f"Session {session_id} timed out")
                yield {
                    "recommendation": timeout_status['message'],
                    "sessionId": None,
                    "requiresNewSession": True,
                    "referralId": referral_id
                }
                return
            
            # Retrieve session context
            session_context = get_session_context(patient_id, session_id)
            logger.info(f"Retrieved session context with {len(session_context)} keys")
        else:
            # Create new session
            session_id = create_session(patient_id, user_message)
            logger.info(f"Created new session: {session_id}")
        
        # Add session context to payload for Supervisor Agent
        enhanced_payload = {
            "query": user_message,
            "patient_id": patient_id,
            "session_id": session_id,
            "session_context": json.dumps(session_context) if session_context else "{}"
        }
        
        logger.info(f"Streaming Supervisor Agent response with session: {session_id}")
        
        # Stream response from Supervisor Agent wrapped with AG-UI event emitter
        stream = supervisor_agent.stream_async(json.dumps(enhanced_payload))
        
        # Create AG-UI event emitter for this run
        run_id = str(uuid.uuid4())
        emitter = AGUIEventEmitter(
            run_id=run_id,
            session_id=session_id,
            thread_id=session_id,
        )
        
        # Wrap stream with AG-UI emitter — yields AG-UI events alongside raw events
        async for event in emitter.wrap_stream(stream, session_context):
            logger.info(f"AG-UI event: {event.get('type', 'unknown')}")
            yield event
        
        # Also collect raw text for session update
        # The emitter already consumed the stream, so we track via the events
        # Extract text from TEXT_MESSAGE_CONTENT events for session history
        # (session update happens via the emitter's text accumulation)
        update_session_activity(patient_id, session_id, user_message, "")
        logger.info(f"Updated session activity for session: {session_id}")
        
    except Exception as e:
        logger.error(f"Error in ClinicalCompass processing: {e}")
        import traceback
        logger.error(traceback.format_exc())
        yield {"error": str(e)}

if __name__ == "__main__":
    print("🚀 Starting ClinicalCompass Multi-Agent System on AgentCore...")
    print("🤖 Entry Agent: Supervisor Agent")
    print("🔗 Architecture: Multi-agent orchestration with conflict resolution")
    print("📋 Specialized Agents:")
    print("   - Patient Context Agent (EHR queries, safety checks)")
    print("   - Global Guidelines Agent (ACR, ESR, NICE, WHO)")
    print("   - Local Regulatory Agent (HAS France, NHS, SOPs)")
    print("⚖️  Precedence: Local > National > International")
    app.run()
