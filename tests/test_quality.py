"""Executable maintainability contracts for production Python sources."""

from __future__ import annotations

import ast
import io
import tokenize
import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
SOURCE_FILES = [
    *sorted((PROJECT / "kisesh").rglob("*.py")),
    *sorted((PROJECT / "integration").glob("*.py")),
]


class QualityContractTests(unittest.TestCase):
    def test_every_module_class_and_function_has_a_real_docstring(self) -> None:
        missing: list[str] = []
        documented_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for path in SOURCE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, documented_nodes) and ast.get_docstring(node) is None:
                    name = getattr(node, "name", "<module>")
                    missing.append(
                        f"{path.relative_to(PROJECT)}:{getattr(node, 'lineno', 1)} {name}"
                    )
        self.assertEqual(missing, [])

    def test_every_function_boundary_has_complete_type_information(self) -> None:
        missing: list[str] = []
        for path in SOURCE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            functions = (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            for function in functions:
                arguments = [
                    *function.args.posonlyargs,
                    *function.args.args,
                    *function.args.kwonlyargs,
                ]
                if function.args.vararg is not None:
                    arguments.append(function.args.vararg)
                if function.args.kwarg is not None:
                    arguments.append(function.args.kwarg)
                for argument in arguments:
                    if argument.arg not in {"self", "cls"} and argument.annotation is None:
                        missing.append(
                            f"{path.relative_to(PROJECT)}:{function.lineno} "
                            f"{function.name}({argument.arg})"
                        )
                if function.returns is None:
                    missing.append(
                        f"{path.relative_to(PROJECT)}:{function.lineno} {function.name} return"
                    )
        self.assertEqual(missing, [])

    def test_production_python_uses_docstrings_instead_of_inline_comments(self) -> None:
        comments: list[str] = []
        for path in SOURCE_FILES:
            source = path.read_text(encoding="utf-8")
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT and not (
                    token.start[0] == 1 and token.string.startswith("#!")
                ):
                    comments.append(f"{path.relative_to(PROJECT)}:{token.start[0]} {token.string}")
        self.assertEqual(comments, [])

    def test_justfile_is_the_only_task_runner_contract(self) -> None:
        self.assertTrue((PROJECT / "justfile").is_file())
        self.assertFalse((PROJECT / "Makefile").exists())
        recipes = (PROJECT / "justfile").read_text(encoding="utf-8")
        self.assertIn("typecheck:", recipes)
        self.assertNotIn("\ntypes:", recipes)

    def test_obsolete_lifecycle_terms_are_absent_from_the_product(self) -> None:
        legacy_module = PROJECT / "kisesh" / "legacy.py"
        roots = (
            PROJECT / "kisesh",
            PROJECT / "integration",
            PROJECT / "bin",
        )
        files = [PROJECT / "README.md", PROJECT / "install"]
        for root in roots:
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path != legacy_module and "__pycache__" not in path.parts
            )

        obsolete = ("park", "undo", "migrate", "migration", "adopt")
        for path in files:
            text = path.read_text(encoding="utf-8").casefold()
            for term in obsolete:
                with self.subTest(path=path, term=term):
                    self.assertNotIn(term, text)

    def test_previous_product_identifiers_are_isolated_to_one_module(self) -> None:
        legacy_module = PROJECT / "kisesh" / "legacy.py"
        old_names = ("kitty-workbench", "kitty_workbench", "KITTY_WORKBENCH")
        production_files = [
            *sorted((PROJECT / "kisesh").glob("*.py")),
            *sorted((PROJECT / "integration").glob("*.py")),
            *sorted((PROJECT / "bin").glob("*")),
            PROJECT / "install",
        ]
        for path in production_files:
            if path == legacy_module:
                continue
            text = path.read_text(encoding="utf-8")
            for old_name in old_names:
                with self.subTest(path=path, old_name=old_name):
                    self.assertNotIn(old_name, text)


if __name__ == "__main__":
    unittest.main()
