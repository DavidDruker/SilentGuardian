import json
from pathlib import Path

from backend.app.main import app

Path('openapi.json').write_text(json.dumps(app.openapi(), indent=2)+'\n', encoding='utf-8')
print('Exported FastAPI schema to openapi.json')
