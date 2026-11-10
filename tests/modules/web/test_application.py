import importlib
import sys
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from alira.modules.web.application import create_app


async def test_web_startup_automatically_registers_module_and_package_routers(tmp_path, monkeypatch):
    routers = importlib.import_module("alira.modules.web.router")
    module_name = f"module_{uuid4().hex}"
    package_name = f"package_{uuid4().hex}"
    (tmp_path / f"{module_name}.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/automatic')\n"
        "@router.get('/module')\n"
        "async def endpoint(): return {'source': 'module'}\n"
    )
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/automatic')\n"
        "@router.get('/package')\n"
        "async def endpoint(): return {'source': 'package'}\n"
    )
    monkeypatch.setattr(routers, "__path__", [str(tmp_path)])
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                for source in ("module", "package"):
                    response = await client.get(f"/automatic/{source}")
                    assert response.status_code == 200
                    assert response.json() == {"source": source}
                response = await client.get("/health")
                assert response.json() == {"status": "ok", "service": "alira"}
    finally:
        sys.modules.pop(f"{routers.__name__}.{module_name}", None)
        sys.modules.pop(f"{routers.__name__}.{package_name}", None)
