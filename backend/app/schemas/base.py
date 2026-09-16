"""Shared schema base classes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def to_camel(name: str) -> str:
    """camelCase that treats the first segment as indivisible.

    Pydantic's stock ``to_camel`` splits on digit boundaries too, so
    ``k8s_namespace`` comes out as ``k8SNamespace``. Every frontend author
    writes ``k8sNamespace``, so the wire format says that instead.
    """
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


class ApiModel(BaseModel):
    """Base for every request/response payload.

    The wire format is camelCase to match the TypeScript frontend while Python
    code stays snake_case. ``populate_by_name`` means request bodies are
    accepted with either spelling, which keeps curl/scripts pleasant.
    ``from_attributes`` lets handlers return ORM objects directly.

    ``extra="forbid"`` is deliberate. Pydantic's default is to *ignore* unknown
    fields, which turns a typo into a silent no-op: posting ``{"groupId": 3}``
    to an endpoint expecting ``groupIds`` returns 200, creates the user, and
    links them to no group at all, with nothing in the response to notice.
    Refusing the body turns that into a 400 naming the offending field, which is
    the only debuggable version of this. It costs nothing on the way out:
    response models are built from ORM attributes and declared fields, never
    from arbitrary dicts.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        extra="forbid",
    )


class ApiMessage(ApiModel):
    """A plain confirmation payload for endpoints with nothing else to return."""

    message: str
    code: str | None = None
