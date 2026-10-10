"""The control room: a window onto the gate, not a product surface over it.

Three panes — the boundary (what's granted), a proposal box (what's being
asked), the action log (what was decided). The centre pane has two entry
points: a natural-language prompt through interpret(), and a direct
proposal straight into dispatch(). Both land on the same gate; the GUI
adds no authority and holds no state.

Boundaries render read-only on purpose. They go in by hand, in psql —
a GUI that can widen a boundary is a GUI that can be talked into it.

Connectors come from the catalogue REGISTRY: whatever actions the host
process registered are what proposals can reach. Run via
scripts/control_room.py, which registers the demo action and serves.
"""

import json
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from .approvals import approve, reject, release
from .catalogue import REGISTRY
from .executor import dispatch
from .interpret import _idempotency_key, interpret
from .models import ActionRequest
from .secrets import resolver_for

_STATIC = Path(__file__).parent / "static"


class Proposal(BaseModel):
    agent: str
    requested_by: str
    action: str
    params: dict
    tenant: str = "default"


class Prompt(BaseModel):
    agent: str
    requested_by: str
    prompt: str
    tenant: str = "default"


class Verdict(BaseModel):
    log_id: int
    verdict: str  # 'approved' | 'rejected'
    by: str
    reason: str | None = None


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="bounds control room")

    def conn():
        # Fresh connection per request: psycopg connections aren't safe
        # under the threadpool's interleaving, and local connects are cheap.
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def connectors():
        return {name: a.fn for name, a in REGISTRY.items()}

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    @app.get("/api/boundaries")
    def boundaries():
        with conn() as c:
            return c.execute(
                """SELECT b.tenant, b.agent, b.version, b.active, b.actions_allowed,
                          b.data_scope, b.limits, b.model_tier, b.created_by,
                          b.created_at, b.credential_ref,
                          c.scope AS credential_scope, c.secret_uri,
                          c.expires_at AS credential_expires_at
                   FROM boundary b
                   LEFT JOIN credential c ON c.ref = b.credential_ref
                   ORDER BY b.agent, b.version DESC"""
            ).fetchall()

    @app.get("/api/log")
    def log(limit: int = 100):
        with conn() as c:
            return c.execute(
                """SELECT id, ts, tenant, trigger_type, trigger_source,
                          requested_by, agent, boundary_version,
                          action_requested, action_params, decision,
                          deny_reason, outcome, data_touched, correlation_id
                   FROM action_log ORDER BY id DESC LIMIT %s""",
                (limit,),
            ).fetchall()

    @app.post("/api/dispatch")
    def propose(p: Proposal):
        req = ActionRequest(
            idempotency_key=_idempotency_key("control-room", p.action, p.params),
            trigger_type="event",
            trigger_source="control-room",
            requested_by=p.requested_by,
            agent=p.agent,
            action=p.action,
            params=p.params,
            data_touched=[],
            tenant=p.tenant,
        )
        with conn() as c:
            out = dispatch(c, req, connectors(), resolver_for(c))
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    @app.post("/api/decide")
    def decide(v: Verdict):
        # Record the human verdict, then finalise. The 004 triggers stand
        # behind both steps — this endpoint has no authority of its own.
        with conn() as c:
            try:
                (approve if v.verdict == "approved" else reject)(
                    c, v.log_id, v.by, v.reason
                )
            except psycopg.errors.RaiseException as e:
                return JSONResponse({"error": str(e.diag.message_primary)}, status_code=409)
            out = release(c, v.log_id, connectors(), resolver_for(c))
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    @app.post("/api/prompt")
    def prompt(p: Prompt):
        # The SDK constructs happily with no key and fails at request time,
        # so the guard has to cover the whole call, not just construction.
        try:
            import anthropic

            client = anthropic.Anthropic()
            with conn() as c:
                out = interpret(
                    c, client,
                    agent=p.agent, requested_by=p.requested_by, prompt=p.prompt,
                    trigger_source="control-room", tenant=p.tenant,
                    connectors=connectors(), resolve_secret=resolver_for(c),
                )
        except (ImportError, TypeError, anthropic.APIError) as e:
            # Model-side failure. Anything the gate decided is already in the
            # log — the log pane stays honest even when this errors.
            return JSONResponse(
                {"error": f"model unavailable: {e}"}, status_code=503
            )
        return JSONResponse(json.loads(json.dumps(out, default=str)))

    return app
