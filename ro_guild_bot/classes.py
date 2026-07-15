from __future__ import annotations

import re


RO_CLASSES: tuple[str, ...] = (
    "Novice",
    "Super Novice",
    "Swordsman",
    "Mage",
    "Archer",
    "Acolyte",
    "Merchant",
    "Thief",
    "Knight",
    "Wizard",
    "Hunter",
    "Priest",
    "Blacksmith",
    "Assassin",
    "Crusader",
    "Sage",
    "Bard",
    "Dancer",
    "Monk",
    "Alchemist",
    "Rogue",
    "High Novice",
    "High Swordsman",
    "High Mage",
    "High Archer",
    "High Acolyte",
    "High Merchant",
    "High Thief",
    "Lord Knight",
    "High Wizard",
    "Sniper",
    "High Priest",
    "Whitesmith",
    "Assassin Cross",
    "Paladin",
    "Professor",
    "Clown",
    "Gypsy",
    "Champion",
    "Creator",
    "Stalker",
    "Taekwon Kid",
    "Star Gladiator",
    "Soul Linker",
    "Ninja",
    "Gunslinger",
)

CLASS_ALIASES: dict[str, str] = {
    "sinx": "Assassin Cross",
    "assassin cross": "Assassin Cross",
    "ws": "Whitesmith",
    "white smith": "Whitesmith",
    "hw": "High Wizard",
    "hp": "High Priest",
    "lk": "Lord Knight",
    "prof": "Professor",
    "bio": "Creator",
    "biochemist": "Creator",
    "creo": "Creator",
    "tk": "Taekwon Kid",
    "taekwon": "Taekwon Kid",
    "taekwon boy": "Taekwon Kid",
    "star glad": "Star Gladiator",
    "star gladiator": "Star Gladiator",
    "soul linker": "Soul Linker",
}


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


_CLASS_BY_KEY = {_key(class_name): class_name for class_name in RO_CLASSES}
_CLASS_BY_KEY.update({_key(alias): canonical for alias, canonical in CLASS_ALIASES.items()})


def resolve_class(value: str) -> str | None:
    """Return the canonical class name for a user-supplied value."""
    return _CLASS_BY_KEY.get(_key(value))


def autocomplete_classes(current: str, limit: int = 25) -> list[str]:
    """Return Discord autocomplete options, prioritizing prefix matches."""
    query = _key(current)
    if not query:
        return list(RO_CLASSES[:limit])

    starts_with = [name for name in RO_CLASSES if _key(name).startswith(query)]
    contains = [name for name in RO_CLASSES if query in _key(name) and name not in starts_with]
    return (starts_with + contains)[:limit]
