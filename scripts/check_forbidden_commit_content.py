from __future__ import annotations

from pathlib import Path
import sys

FORBIDDEN_STRINGS: tuple[str, ...] = (
    "products:" + "\n",
    "daemon:" + "\n",
    "".join(("nsec_", "bitcoin_safe_pos")),
)


def find_matches(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    return [needle for needle in FORBIDDEN_STRINGS if needle in content]


def main(argv: list[str]) -> int:
    has_errors = False

    for file_name in argv[1:]:
        path = Path(file_name)
        matches = find_matches(path)
        if not matches:
            continue

        has_errors = True
        forbidden_values = ", ".join(repr(match) for match in matches)
        print(f"{path}: contains forbidden content: {forbidden_values}")

    if has_errors:
        print("Commit blocked. Remove the forbidden content before committing.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
