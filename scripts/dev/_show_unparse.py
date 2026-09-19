"""Show how the paintEvent guards render after ast.unparse, to check the patterns."""

import ast
import pathlib

src = pathlib.Path("src/metixel/display/qt_canvas.py").read_text(encoding="utf-8")
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == "paintEvent":
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        text = "\n".join(ast.unparse(s) for s in body)
        for line in text.splitlines():
            if "ambient_strategy" in line:
                print(repr(line))
        break
