from pathlib import Path


def load_local_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    roots = [Path.cwd(), *Path(__file__).resolve().parents]
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        load_dotenv(root / ".env.local")
        load_dotenv(root / ".env")


load_local_environment()
