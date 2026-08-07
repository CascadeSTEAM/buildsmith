"""Design tokens: the manifest, the applied map, and the plan between them.

Three ideas, and keeping them apart is most of the value here:

**Intent** is the manifest — what the design system says the tokens should be,
keyed by a stable logical name we own.

**Applied** is what is actually live, read back from the site: logical name →
the record's uuid and current values. Builder assigns that uuid and we never
choose it (see `DOCTYPE_NAMING`).

**The plan** is the difference, expressed as operations that are safe to apply —
never as a desired-state overwrite, because the one thing you must not do to a
`Builder Variable` is delete it (TRAP-007).

Intent and applied are never the same file and never merged into one. A page
holds `var(--uuid, literal)` references; only the applied map knows the uuid, so
only the applied map can emit a reference.

Nothing here touches a site. `plan()` produces a payload; applying it is
somebody else's job.

Verified against the pinned Builder commit (`sandbox/pins.env`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "DOCTYPE",
    "NOT_TOKENISABLE",
    "Applied",
    "Manifest",
    "Operation",
    "Plan",
    "Token",
    "TokenError",
    "TokenType",
    "assert_tokenisable",
    "validate_styles",
    "plan",
]


class TokenError(ValueError):
    """A token definition or plan would fail, silently or otherwise."""


#: The doctype name, in exactly one place. Upstream renamed `Builder Variable`
#: to `Builder Token` in `f0781da9` (2026-07-16) — after the current pin, so this
#: is correct today, and moving the pin past that commit makes the migration a
#: one-line change here rather than a sweep (TRAP-011).
DOCTYPE = "Builder Variable"

TokenType = Literal["Color", "Dimension"]

#: The doctype's `type` field is a Select with exactly these options. There is no
#: escape hatch and no third value (TRAP-004).
TOKEN_TYPES: frozenset[str] = frozenset({"Color", "Dimension"})

#: Why we never choose a token's `name`. Builder's `autoname()` assigns
#: `str(uuid.uuid4())` only when the field is empty — so a name *could* be
#: supplied, and it is tempting, because `--brand-primary` reads better in a
#: stylesheet than `--0f1e...`. Do not. Upstream ships
#: `builder/patches/refactor_builder_variables.py`, which finds every
#: non-uuid-named variable, renames it to a fresh uuid, and rewrites
#: `var(--old-name)` to `var(--uuid)` across every page and component. A readable
#: name is not a stable one — it survives until the next `bench migrate`.
DOCTYPE_NAMING = "uuid, assigned by Builder — opaque and externally owned"

#: CSS properties people reach for and cannot tokenise, mapped to the reason, so
#: the error says what to do instead rather than only what not to do. Keys are
#: camelCase to match Builder's style dicts (`baseStyles`, `mobileStyles`, ...).
NOT_TOKENISABLE: dict[str, str] = {
    "animation": "an animation shorthand is a composite value",
    "boxShadow": "a shadow is a composite value, not a single Color or Dimension",
    "fontFamily": "a font stack is not a Color or a Dimension",
    "fontStyle": "a keyword is neither a Color nor a Dimension",
    "fontWeight": "a weight is a unitless number, not a Dimension",
    "lineHeight": "a unitless line-height is not a Dimension",
    "textShadow": "a shadow is a composite value",
    "textTransform": "a keyword is neither a Color nor a Dimension",
    "transition": "an easing curve is not a Color or a Dimension",
    "transitionTimingFunction": "an easing curve is not a Color or a Dimension",
}


@dataclass(frozen=True)
class Token:
    """One token, as *intent*. Carries no uuid — that is the applied map's job."""

    key: str
    value: str
    type: TokenType = "Color"
    dark_value: str | None = None
    group: str | None = None
    label: str | None = None  # the record's `variable_name`; defaults to `key`

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise TokenError(f"token key must be a non-empty string, got {self.key!r}")

        if self.type not in TOKEN_TYPES:
            raise TokenError(
                f"{self.key}: type must be one of {sorted(TOKEN_TYPES)}, got {self.type!r}. "
                f"`{DOCTYPE}.type` is a Select with no third option — anything else has to "
                "become a component prop plus one injected head_html stylesheet (TRAP-004)."
            )

        # `value` is REQD on the doctype, but the sharper reason is that
        # get_css_variables() skips any variable with a falsy value: no CSS
        # variable is emitted, and every var(--uuid, literal) reference quietly
        # falls back to its literal. The design would look *almost* right.
        if not self.value or not str(self.value).strip():
            raise TokenError(
                f"{self.key}: value must be non-empty. Builder emits no CSS variable for an "
                "empty value, so every reference silently falls back to its literal."
            )

        # Builder composes light-dark() itself from value + dark_value, and only
        # when they differ. Pre-composing it here would nest one inside another.
        for name, val in (("value", self.value), ("dark_value", self.dark_value)):
            if val and "light-dark(" in str(val):
                raise TokenError(
                    f"{self.key}: {name} already contains light-dark(). Builder composes that "
                    "from value and dark_value when they differ — supply the plain values."
                )

    @property
    def variable_name(self) -> str:
        """The human-facing label stored on the record."""
        return self.label or self.key

    def record(self) -> dict[str, Any]:
        """The doctype payload. Deliberately omits `name` — see DOCTYPE_NAMING.

        `is_standard` is also omitted, which leaves it at the doctype default of
        0. Setting it makes Builder export the record to files on every update;
        that is a fixtures concern, not a design-token one.
        """
        payload: dict[str, Any] = {
            "doctype": DOCTYPE,
            "variable_name": self.variable_name,
            "type": self.type,
            "value": self.value,
        }
        # Equal values are not a dark mode — Builder emits a plain declaration
        # when they match, so storing a duplicate only invites later confusion
        # about whether someone meant them to diverge.
        if self.dark_value and self.dark_value != self.value:
            payload["dark_value"] = self.dark_value
        if self.group:
            payload["group"] = self.group
        return payload


def assert_tokenisable(css_property: str) -> None:
    """Raise if a CSS property cannot be expressed as a token. TRAP-004."""
    reason = NOT_TOKENISABLE.get(css_property)
    if reason:
        raise TokenError(
            f"'{css_property}' cannot be a token: {reason}. `{DOCTYPE}.type` is only "
            "Color or Dimension. Make it a component prop and inject one head_html "
            "stylesheet instead (TRAP-004)."
        )


def validate_styles(styles: dict[str, Any], *, path: str = "styles") -> None:
    """Check a Builder style dict for properties that cannot hold a token.

    Only flags a property when its value actually references one. A literal
    `fontWeight: 600` is perfectly fine — the trap is believing it can be a
    token, and that only shows up once someone writes `var(...)` into it.
    """
    for prop, value in styles.items():
        if prop in NOT_TOKENISABLE and isinstance(value, str) and "var(--" in value:
            raise TokenError(
                f"{path}.{prop} holds a token reference, but {NOT_TOKENISABLE[prop]}. "
                f"`{DOCTYPE}.type` is only Color or Dimension — make it a component prop "
                "plus one injected head_html stylesheet (TRAP-004)."
            )


@dataclass
class Manifest:
    """Design intent: the tokens the system should have, keyed by logical name."""

    tokens: dict[str, Token] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        meta = raw.get("_meta", {})
        entries = raw.get("tokens", {k: v for k, v in raw.items() if not k.startswith("_")})

        tokens: dict[str, Token] = {}
        for key, spec in entries.items():
            if not isinstance(spec, dict):
                raise TokenError(f"{key}: expected an object, got {type(spec).__name__}")
            unknown = set(spec) - {"value", "type", "dark_value", "group", "label"}
            if unknown:
                raise TokenError(f"{key}: unknown field(s) {sorted(unknown)}")
            tokens[key] = Token(key=key, **spec)

        # `variable_name` is the only field tying a live record back to a logical
        # key — the record's own name is an opaque uuid. Two tokens sharing a
        # label make reading the applied map back from the site ambiguous, and
        # the ambiguity surfaces as tokens silently swapping uuids.
        labels: dict[str, str] = {}
        for key, token in tokens.items():
            clash = labels.get(token.variable_name)
            if clash:
                raise TokenError(
                    f"tokens '{clash}' and '{key}' share the variable_name "
                    f"{token.variable_name!r}. That is the only field linking a live record "
                    "back to a logical key, so duplicates make read-back ambiguous."
                )
            labels[token.variable_name] = key

        return cls(tokens=tokens, meta=meta)

    @classmethod
    def load(cls, path: str | Path) -> Manifest:
        return cls.from_dict(json.loads(Path(path).read_text()))

    def __getitem__(self, key: str) -> Token:
        try:
            return self.tokens[key]
        except KeyError:
            raise TokenError(f"no token '{key}' in the manifest") from None

    def __contains__(self, key: str) -> bool:
        return key in self.tokens

    def __len__(self) -> int:
        return len(self.tokens)


@dataclass
class Applied:
    """What is actually live: logical key → the record's uuid and current values.

    Read back from the site, never authored by hand. This is the only object that
    can emit a `var()` reference, because it is the only one that knows a uuid.
    """

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Applied:
        entries = raw.get("tokens", {k: v for k, v in raw.items() if not k.startswith("_")})
        for key, entry in entries.items():
            missing = {"uuid", "value"} - set(entry)
            if missing:
                raise TokenError(f"applied token '{key}' is missing {sorted(missing)}")
        return cls(entries=entries)

    @classmethod
    def load(cls, path: str | Path) -> Applied:
        return cls.from_dict(json.loads(Path(path).read_text()))

    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def _entry(self, key: str) -> dict[str, Any]:
        try:
            return self.entries[key]
        except KeyError:
            raise TokenError(
                f"no applied token '{key}'. References need the uuid Builder assigned, so it "
                "must be read back from the site — there is nothing to fall back to, and "
                "inventing one would produce a reference that resolves to nothing."
            ) from None

    def uuid(self, key: str) -> str:
        return self._entry(key)["uuid"]

    def ref(self, key: str) -> str:
        """`var(--<uuid>, <literal>)`.

        The literal fallback is the point: if the variable is missing or empty at
        render time, the property degrades to the right value instead of to
        nothing. A bare `var(--uuid)` fails invisibly.

        The fallback is the *live* value, not the manifest's, because it has to
        describe what the site will actually render. Call `assert_in_sync()`
        first when composing, or a pending plan will bake stale literals in.
        """
        entry = self._entry(key)
        return f"var(--{entry['uuid']}, {entry['value']})"

    def literal(self, key: str) -> str:
        """The plain value, for the places a `var()` cannot go."""
        return self._entry(key)["value"]

    def assert_in_sync(self, manifest: Manifest) -> None:
        """Refuse to compose against a site that has not caught up with intent.

        Composition bakes the live value into every reference as its fallback.
        Doing that while a plan is still unapplied writes yesterday's colours
        into today's components — and they only show up when a variable is
        missing, which is exactly when nobody is looking.
        """
        operations, _ = _diff(manifest, self)
        if operations:
            summary = "\n  ".join(str(op) for op in operations[:10])
            more = f"\n  ... and {len(operations) - 10} more" if len(operations) > 10 else ""
            raise TokenError(
                f"the applied map is {len(operations)} operation(s) behind the manifest, so "
                f"composing now would embed stale literals:\n  {summary}{more}\n"
                "Apply the plan and read the token map back before composing."
            )


@dataclass(frozen=True)
class Operation:
    """One safe change to reach intent from what is live.

    There is deliberately no `delete`. Deleting a variable orphans every
    `var(--uuid)` reference to it — one page alone was found holding 50 — and
    nothing cascades or warns (TRAP-007).
    """

    kind: Literal["mint", "set_value", "set_dark_value", "rename", "set_group"]
    key: str
    uuid: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.kind:15} {self.key:30} {self.uuid or '(new)'}  {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "uuid": self.uuid,
            "payload": self.payload,
            "reason": self.reason,
        }


@dataclass
class Plan:
    """The emitted payload: what to change, and what was deliberately left alone."""

    operations: list[Operation] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)
    doctype: str = DOCTYPE

    def __len__(self) -> int:
        return len(self.operations)

    def __bool__(self) -> bool:
        return bool(self.operations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doctype": self.doctype,
            "operations": [op.to_dict() for op in self.operations],
            # Carried in the payload so whoever applies it sees them and does
            # *not* tidy them up. Their absence from `operations` is the
            # instruction; listing them is so the omission looks deliberate.
            "orphans": self.orphans,
            "notes": [
                "Never delete a Builder Variable — references do not cascade (TRAP-007).",
                "Record names are uuids assigned by Builder; never supply one.",
            ],
        }

    def write(self, path: str | Path) -> Path:
        """Emit the plan as JSON. Applying it is somebody else's job."""
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    def summary(self) -> str:
        if not self.operations and not self.orphans:
            return "token plan: nothing to do — the site matches the manifest"
        lines = [f"token plan: {len(self.operations)} operation(s)"]
        lines += [f"  {op}" for op in self.operations]
        if self.orphans:
            lines.append(f"  {len(self.orphans)} live token(s) not in the manifest, left alone:")
            lines += [f"    {o['key']}  {o['uuid']}" for o in self.orphans]
        return "\n".join(lines)


def _diff(manifest: Manifest, applied: Applied) -> tuple[list[Operation], list[dict[str, Any]]]:
    """Shared by `plan()` and `Applied.assert_in_sync()`."""
    operations: list[Operation] = []
    conflicts: list[str] = []

    for key, token in manifest.tokens.items():
        if key not in applied:
            operations.append(
                Operation("mint", key, None, token.record(), "not present on the site")
            )
            continue

        live = applied.entries[key]
        uuid = live["uuid"]

        # A type change reinterprets every reference to the token, so it is a
        # decision, not a diff outcome. Collected rather than raised on sight, so
        # one run reports every conflict instead of one per attempt.
        if live.get("type") and live["type"] != token.type:
            conflicts.append(
                f"  {key}: live type is {live['type']!r}, manifest says {token.type!r}"
            )
            continue

        if str(live.get("value")) != str(token.value):
            operations.append(
                Operation(
                    "set_value", key, uuid,
                    {"value": token.value},
                    f"{live.get('value')!r} -> {token.value!r}",
                )
            )

        wanted_dark = token.dark_value if token.dark_value != token.value else None
        if (live.get("dark_value") or None) != wanted_dark:
            operations.append(
                Operation(
                    "set_dark_value", key, uuid,
                    {"dark_value": wanted_dark},
                    f"{live.get('dark_value')!r} -> {wanted_dark!r}",
                )
            )

        # A rename is a label change on the same record. The uuid — and so every
        # reference to it — is untouched. This is the *only* safe way to rename.
        if live.get("variable_name") and live["variable_name"] != token.variable_name:
            operations.append(
                Operation(
                    "rename", key, uuid,
                    {"variable_name": token.variable_name},
                    f"in place: {live['variable_name']!r} -> {token.variable_name!r}",
                )
            )

        if token.group and live.get("group") != token.group:
            operations.append(
                Operation("set_group", key, uuid, {"group": token.group}, "regrouped")
            )

    if conflicts:
        raise TokenError(
            "token type conflicts, which change the meaning of every reference:\n"
            + "\n".join(conflicts)
            + "\nResolve these deliberately rather than letting a diff decide."
        )

    orphans = [
        {"key": key, "uuid": entry.get("uuid"), "value": entry.get("value")}
        for key, entry in applied.entries.items()
        if key not in manifest
    ]
    return operations, orphans


def plan(manifest: Manifest, applied: Applied) -> Plan:
    """Diff intent against live state and return an applyable plan.

    Orphans — live tokens the manifest no longer mentions — are reported and
    **never** turned into operations. Retiring one is a human decision made after
    auditing its references, not something a diff should infer (TRAP-007).
    """
    operations, orphans = _diff(manifest, applied)
    return Plan(operations=operations, orphans=orphans)
