#!/usr/bin/env python3
"""Startup script for NewsForge application.

Usage:
    # Development (local):
    python -m uvicorn src.newsforge.web.app:app --reload

    # Production (PostgreSQL + real AI; the fail-fast configuration gate runs at
    # startup and REFUSES to boot on any unsafe/missing value):
    NEWSFORGE_ENVIRONMENT=production \
    NEWSFORGE_DATABASE_URL=postgresql+pg8000://user:pass@host/newsforge \
    NEWSFORGE_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
    NEWSFORGE_MOCK_AI=false \
    NEWSFORGE_DEFAULT_PROVIDER=openai \
    NEWSFORGE_OPENAI_API_KEY=<key> \
    NEWSFORGE_SITE_URL=https://newsforge.example \
    python -m uvicorn src.newsforge.web.app:app --host 0.0.0.0 --port 8000

    # See PRODUCTION_READINESS.md for the full contract, and the deployment smoke
    # script (scripts/deploy_smoke.py) for post-deployment verification.

Health check endpoint: GET http://localhost:8000/health
"""

import os
import sys
from pathlib import Path


def main():
    """Main entry point for NewsForge startup."""
    
    # Set Python path if not already set
    src_dir = Path(__file__).parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    
    # Load environment from template if it exists and doesn't conflict
    env_template = Path(__file__).parent / ".env.template"
    env_file = Path(__file__).parent / ".env"
    
    if env_file.exists() and not env_file.is_symlink():
        print("Loading .env configuration...")
        # Environment file is already loaded via standard mechanisms
    
    elif env_template.exists():
        print("Using .env.template as .env...")
        import shutil
        shutil.copy(env_template, env_file)
    
    print("=" * 60)
    print("NewsForge Application Starting")
    print("=" * 60)
    print()
    
    # Show configuration (without secrets)
    config_vars = [
        "NEWSFORGE_DB_PATH",
        "NEWSFORGE_MOCK_AI", 
        "NEWSFORGE_DEFAULT_PROVIDER",
        "NEWSFORGE_SITE_URL",
        "NEWSFORGE_HOST",
        "NEWSFORGE_PORT",
    ]
    
    for var in config_vars:
        value = os.getenv(var, "<not set>")
        # Mask API keys
        if "KEY" in var or "SECRET" in var:
            value = "***"
        print(f"  {var}: {value}")
    
    print()
    print("Application ready at:")
    host = os.getenv("NEWSFORGE_HOST", "0.0.0.0")
    port = os.getenv("NEWSFORGE_PORT", "8000")
    url = f"http://{host}:{port}"
    print(f"  {url}")
    print()
    print("Endpoints:")
    print(f"  GET /health     - Health check")
    print(f"  GET /articles   - Published articles list")
    print(f"  GET /sitemap.xml - Sitemap")
    print(f"  GET /feed.xml   - RSS feed")
    print()
    
    print("For more info, see: https://docs.newsforge.local/")
    print("=" * 60)
    print()
    

if __name__ == "__main__":
    main()