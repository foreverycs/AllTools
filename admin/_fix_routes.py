"""One-shot fix for admin/routes.py syntax (missing closing parens)."""
from pathlib import Path

p = Path(__file__).resolve().parent / "routes.py"
text = p.read_text(encoding="utf-8")

replacements = [
    (
        'router = APIRouter(prefix="/admin", tags=["admin"]\n',
        'router = APIRouter(prefix="/admin", tags=["admin"])\n',
    ),
    (
        'RedirectResponse(url="/admin/login", status_code=303)',
        'RedirectResponse(url="/admin/login", status_code=303))',
    ),
]
for old, new in replacements:
    if old in text:
        text = text.replace(old, new)
        print("replaced:", old[:50])

# Avoid double-closing already fixed redirects
while "status_code=303)))" in text:
    text = text.replace("status_code=303)))", "status_code=303))")

p.write_text(text, encoding="utf-8")
import ast

ast.parse(text)
print("PARSE OK")
