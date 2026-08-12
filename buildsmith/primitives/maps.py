"""A location/map embed block: address in, responsive iframe out.

OpenStreetMap is the default, Google Maps an opt-in alternative — mirroring the
project's FOSS-first stance. The two providers are not symmetric, and that
asymmetry drives this module's shape:

**Google** resolves a free-text address itself, server-side, at the moment the
iframe loads: `maps.google.com/maps?q=<address>&output=embed` needs no API key
and no lookup on our side. "Address in" is literal for this provider.

**OpenStreetMap**'s own sanctioned embed — the URL its "Share" panel
generates — is `export/embed.html?bbox=...&marker=<lat>,<lon>`. It has no
free-text mode; it needs coordinates. Turning an address into coordinates is
geocoding, which means an HTTP client, and `primitives/` structurally has none
(see `pyproject.toml`: "Nothing in primitives/ may take a dependency on an HTTP
client"). So the OSM path asks the caller for `lat`/`lon` — looked up once,
by hand, via OSM's own Share panel or any geocoder — rather than reaching for
one quietly. `EmbedError` says so when they are missing, rather than the
component silently falling back to Google or to no map at all.

Emits a single `iframe` block — not a wrapper `div` around one; see TRAP-019
in `location_map()`'s docstring — with `@token` sigils for the properties the
issue asked to be themeable (sizing, border), exactly like every other
component spec under `design/components/*.json`. Resolving those sigils and
composing the result into a `Builder Component` is `build_site()`'s job, same
as `site-header.json`/`site-footer.json`; this module only builds the tree.

Nothing here touches a site, and nothing here touches the network.
"""

from __future__ import annotations

import urllib.parse

from buildsmith.primitives.blocks import BlockError, new_block, validate

__all__ = [
    "DEFAULT_SPAN",
    "PROVIDERS",
    "EmbedError",
    "google_embed_src",
    "location_map",
    "osm_embed_src",
]


class EmbedError(BlockError):
    """A map embed cannot be built as asked."""


#: Half-width of the OSM bounding box, in degrees. ~0.006 deg is a few hundred
#: metres at most inhabited latitudes — close enough to read a street-level
#: marker without the caller having to think in degrees. Override via `span=`.
DEFAULT_SPAN = 0.006

PROVIDERS: frozenset[str] = frozenset({"osm", "google"})


def osm_embed_src(lat: float, lon: float, *, span: float = DEFAULT_SPAN) -> str:
    """The URL OpenStreetMap's own "Share > Embed" panel would hand you.

    `layer=mapnik` is the default tile layer; `marker` drops the pin the panel
    always includes when a location (rather than just a view) was shared.

    The bbox is clamped to OSM's valid range after `span` is applied, not just
    the centre point: a marker within `span` of a pole, or of the antimeridian,
    would otherwise expand into a bbox edge past ±90/±180 — invalid, not just
    off-centre. Real places sit this close to both (Chukotka straddles the
    antimeridian; Ross Island is inside the Antarctic Circle), so this is not a
    hypothetical input, only an unlikely one.
    """
    if not -90 <= lat <= 90:
        raise EmbedError(f"lat must be between -90 and 90, got {lat!r}")
    if not -180 <= lon <= 180:
        raise EmbedError(f"lon must be between -180 and 180, got {lon!r}")
    if span <= 0:
        raise EmbedError(f"span must be positive, got {span!r}")

    min_lat = max(-90.0, lat - span)
    max_lat = min(90.0, lat + span)
    min_lon = max(-180.0, lon - span)
    max_lon = min(180.0, lon + span)

    params = {
        "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "layer": "mapnik",
        "marker": f"{lat},{lon}",
    }
    return "https://www.openstreetmap.org/export/embed.html?" + urllib.parse.urlencode(params)


def google_embed_src(address: str) -> str:
    """The undocumented-but-stable no-key Google Maps embed: `output=embed`.

    Google resolves `address` itself when the iframe loads — nothing here
    geocodes it.
    """
    if not address or not address.strip():
        raise EmbedError(
            "address must be non-empty — Google resolves it server-side when the "
            "iframe loads, so an empty address embeds nothing."
        )
    return "https://maps.google.com/maps?" + urllib.parse.urlencode(
        {"q": address, "output": "embed"}
    )


def location_map(
    *,
    address: str,
    provider: str = "osm",
    lat: float | None = None,
    lon: float | None = None,
    span: float = DEFAULT_SPAN,
    width: str = "@map-width",
    height: str = "@map-height",
    border_radius: str = "@map-radius",
    border_color: str = "@map-border",
    border_width: str = "1px",
) -> dict:
    """Build the block tree for a location map: a bare `iframe`, and nothing wrapping it.

    `address` is always required — it becomes the iframe's accessible `title`,
    and for `provider="google"` it is also the query the embed resolves. It is
    never geocoded here.

    **The `iframe` is the root block, not a child of a wrapping `div`.**
    That was not the first shape this took — a `div > iframe` tree passed
    every check this module had (`blocks.validate()`, the colour-tokenisation
    check, a real `buildsmith load` into the pinned sandbox with a byte-exact
    DB read-back) and still rendered as a blank box in the Builder *editor*
    canvas, live coordinates confirmed by extending it onto a real page and
    opening `/builder/page/...` in a browser. A control test against
    Builder's own built-in YouTube block — also an iframe, authored directly
    on a page rather than through component-extension — rendered live with no
    trouble. Flattening this component to root on the `iframe` itself (proven
    the same way, screenshot included) fixed it. See TRAP-019.

    Sizing and border are longhand CSS properties, not the `border` shorthand:
    `assert_colours_tokenised` (components.py) already documents why a
    shorthand is where a literal colour hides unnoticed, and a longhand
    property is either a full `@token` reference or a plain non-colour
    literal — never a mix a token-resolution pass would have to parse apart.
    `width`/`height`/`border_radius`/`border_color` default to `@map-*` sigils
    so the component themes with the site by default; pass a plain literal
    (e.g. `width="400px"`) for a site that would rather not mint new tokens.
    No `overflow: hidden` is needed to make the radius take — a browser clips
    an iframe's own rendered box to it directly, the same as it would an
    `img` or `video` (confirmed in the same screenshot: the pin and tiles stay
    inside the rounded corners with no wrapper at all).

    Runs `blocks.validate()` before returning, so a structural mistake fails
    here rather than downstream. Colour-tokenisation is not checked — that is
    `compose()`'s job once the caller has an `Applied` map — and no blockIds
    are assigned; the returned tree is exactly the shape `compose()` expects
    as `root`.
    """
    if not address or not address.strip():
        raise EmbedError("address must be non-empty — it becomes the embed's accessible title.")
    if provider not in PROVIDERS:
        raise EmbedError(f"provider must be one of {sorted(PROVIDERS)}, got {provider!r}")

    if provider == "google":
        src = google_embed_src(address)
    else:
        if lat is None or lon is None:
            raise EmbedError(
                "OpenStreetMap's embed needs coordinates, not just an address. buildsmith "
                "does not geocode — primitives/ takes no HTTP-client dependency — so look "
                "the address up once via OpenStreetMap's own Share panel or any geocoder and "
                "pass lat=/lon=, or pass provider='google', which resolves the address itself."
            )
        src = osm_embed_src(lat, lon, span=span)

    root = new_block(
        "iframe",
        attributes={
            "src": src,
            "loading": "lazy",
            "title": f"Map: {address}",
        },
        base_styles={
            "width": width,
            "height": height,
            "borderRadius": border_radius,
            "borderWidth": border_width,
            "borderStyle": "solid",
            "borderColor": border_color,
        },
    )
    validate(root)
    return root
