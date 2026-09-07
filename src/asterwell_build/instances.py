"""Cut the static instances out of the assembled variable fonts (§7.5).

Sixteen TTFs — the eight named instances of each variable font — are what a
host that cannot use a variable font gets, and what an export path hands to a
printer. Every one of them is pinned at ``opsz=12``, Literata's own default and
the only optical size its named instances use, so the family's static face is
the text face; the other optical sizes stay a variable-font feature.

``fontTools.varLib.instancer`` does the outline work: it applies each glyph's
``gvar`` deltas at the pinned location, applies ``MVAR`` to the metrics that
vary (cap height, x-height, strikeout — never the line metrics, which is why
the vertical metrics come through frozen), and drops ``fvar``, ``avar``,
``gvar``, ``MVAR``, ``HVAR`` and ``VVAR``. ``STAT`` is deliberately kept: it is
what tells a host how this file sits in the family.

What the instancer does *not* reliably own is the style identity, so this module
writes it rather than checking it:

* **The bits.** ``OS/2.usWeightClass`` is the instance's own weight;
  ``fsSelection`` carries BOLD at 700, ITALIC in the italic and REGULAR in every
  upright that is not the Bold (see :attr:`Instance.fs_selection_bits`);
  ``head.macStyle`` says the same thing in the two bits it has. Everything else
  in ``fsSelection`` — USE_TYPO_METRICS above all — is masked through untouched.
* **The names.** IDs 3 and 6 are built from the *family's* PostScript name, not
  from the italic variable font's name ID 25 (``AsterwellTextItalic``), which is
  what the instancer would otherwise leave behind as
  ``AsterwellTextItalic-SemiBoldItalic``. IDs 1/2 follow the RIBBI rule and IDs
  16/17 appear only where they are needed, both spelled out here rather than
  inferred, so the naming does not depend on a ``STAT`` flag being right. ID 25
  itself is removed: it describes a variable font, and this one is not.

That last point is worth stating plainly, because it *is* right: the ``opsz=12``
axis value is marked elidable in Step 6 (D19), and the instancer does therefore
name the roman 700 instance "Asterwell Text Bold" rather than "Asterwell Text
12pt Bold". This module writes the same strings anyway — a check that agrees
with the thing it checks is worth having, and a family that renames itself when
a flag flips is not.

Finally, each written file is reopened and measured — §9.1's metrics freeze,
applied to a static. The vertical metrics, the units per em, the layout feature
lists and every ``fsSelection`` bit that is not a style bit must be the variable
font's; the glyph count must be too. What a static *cannot* be held to is the
``head`` bounding box, and :func:`frozen_differences` says why at length: a
variable font's box is its default instance's, so Bold's is legitimately bigger.
Those numbers are reported per file instead.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

from asterwell_build import assemble
from asterwell_build.assemble import Family, Invariants, Log, StyleReport

__all__ = [
    "Instance",
    "InstanceError",
    "StaticReport",
    "apply_names",
    "apply_style_bits",
    "build_static",
    "build_statics",
    "build_style_statics",
    "frozen_differences",
    "name_strings",
    "named_instances",
    "output_name",
]


class InstanceError(assemble.AssembleError):
    """A variable font cannot be cut into the statics the family promises."""


#: Optical size every static is pinned at: Literata's default, and the only
#: value its eight named instances use (§14.1). The other optical sizes remain
#: a variable-font feature; per-``opsz`` static families are out of scope (§11).
STATIC_OPSZ = 12

#: The two axes a named instance is pinned on. ``ital`` is a ``STAT``-only axis
#: here — the two styles are separate files, not two ends of one axis.
PINNED_AXES = ("opsz", "wght")

#: ``OS/2.fsSelection`` bits this module owns. Every other bit — USE_TYPO_METRICS
#: (7) above all, which the metrics freeze depends on — is masked through.
FS_ITALIC = 1 << 0
FS_BOLD = 1 << 5
FS_REGULAR = 1 << 6
FS_USE_TYPO_METRICS = 1 << 7

#: ``head.macStyle`` bits, the same two facts in the older table.
MAC_BOLD = 1 << 0
MAC_ITALIC = 1 << 1

#: The weight that carries a style bit of its own: 700 is the family's Bold,
#: the only weight a host reaches through the bold button. Every other weight is
#: addressed by name.
BOLD_WEIGHT = 700

#: ``name`` records that describe a *variable* font and mean nothing in a static
#: one. ID 25 is the variations PostScript-name prefix, defined by the OpenType
#: spec for variable fonts only; the instancer leaves it behind and Literata's
#: own statics do not carry it.
VARIABLE_ONLY_NAME_IDS = (25,)

#: The four styles a host may address through name IDs 1 and 2 alone. For these
#: — and only these — the legacy family name is the family's own and IDs 16/17
#: are not written; every other weight needs the typographic pair.
RIBBI_SUBFAMILIES = ("Regular", "Italic", "Bold", "Bold Italic")

#: Suffix that marks an italic subfamily name, and what is taken off it to get
#: the weight name that goes into name ID 1.
ITALIC_SUFFIX = "Italic"

#: Typographic family/subfamily. Written for a non-RIBBI instance, removed for a
#: RIBBI one, where name IDs 1 and 2 already say everything they would.
TYPOGRAPHIC_NAME_IDS = (16, 17)

#: Fields of :class:`~asterwell_build.assemble.Invariants` a static instance
#: cannot carry, because instancing removes the table each is read from.
VARIATION_FIELDS = ("fvar_axes", "fvar_instances", "mvar", "avar")

#: The one field a static is *meant* to differ in: ``fsSelection`` is where the
#: style bits live, so it is compared bit by bit (see :func:`frozen_differences`)
#: instead of whole.
RESTYLED_FIELDS = ("fs_selection",)

#: Fields that describe how big the font is, recomputed on save from whatever
#: outlines are actually there. A variable font's are its *default instance's*,
#: so a static's move in both directions and by design. Reported per file
#: (:class:`StaticReport`) rather than frozen; :func:`frozen_differences` says
#: why at length.
MEASURED_FIELDS = ("head_bbox", "advance_width_max")

#: Layout features that exist only while the font varies. ``rvrn`` swaps in
#: alternates for a region of the design space; once the location is pinned the
#: instancer has applied it and the feature has nothing left to do, so it may
#: legitimately be gone. Any *other* feature going missing is a loss of ability.
VARIATION_FEATURES = ("rvrn",)


# --------------------------------------------------------------------------- #
# One named instance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Instance:
    """One named instance of a variable font, and the identity its static gets."""

    weight: int
    """``wght`` the instance is pinned at; also ``OS/2.usWeightClass``."""

    subfamily: str
    """The instance's own subfamily name, spaces and all: ``SemiBold Italic``."""

    italic: bool
    """Whether the variable font this came from is the italic one."""

    @property
    def token(self) -> str:
        """``<Style>``: the subfamily without spaces, for filenames and
        PostScript names — ``SemiBoldItalic``, ``Italic``, ``Regular``."""
        return self.subfamily.replace(" ", "")

    @property
    def ribbi(self) -> bool:
        return self.subfamily in RIBBI_SUBFAMILIES

    @property
    def weight_name(self) -> str:
        """The subfamily without its italic suffix: ``SemiBold Italic`` →
        ``SemiBold``. This is the part that belongs in the legacy family name,
        because the italic half is what name ID 2 is for."""
        return self.subfamily.removesuffix(ITALIC_SUFFIX).strip()

    @property
    def fs_selection_bits(self) -> int:
        """The three ``fsSelection`` bits this instance sets; the rest are the
        variable font's.

        REGULAR is the one worth spelling out. The OpenType spec defines bit 6
        as "font is regular: not italic, not bold, not oblique", and says
        nothing about weight — so every upright non-Bold static sets it,
        SemiBold and Black included, which is also what their name ID 2 says
        ("Regular", the other half of the pair whose ID 1 is "Asterwell Text
        SemiBold"). Literata's own 62 statics do exactly this, and fontbakery's
        ``opentype/fsselection`` requires it. §7.5's "iff w == 400" would leave
        the six upright non-RIBBI statics with no style bit at all; see the
        Step 7 report.
        """
        bits = 0
        if self.weight == BOLD_WEIGHT:
            bits |= FS_BOLD
        if self.italic:
            bits |= FS_ITALIC
        if not self.italic and self.weight != BOLD_WEIGHT:
            bits |= FS_REGULAR
        return bits

    @property
    def mac_style_bits(self) -> int:
        bits = 0
        if self.weight == BOLD_WEIGHT:
            bits |= MAC_BOLD
        if self.italic:
            bits |= MAC_ITALIC
        return bits

    @property
    def location(self) -> dict[str, int]:
        """Where the instancer pins the design space for this static."""
        return {"opsz": STATIC_OPSZ, "wght": self.weight}


def named_instances(vf: TTFont) -> list[Instance]:
    """The variable font's ``fvar`` named instances, in ``fvar`` order.

    Italic-ness is read from the font (``fsSelection``) rather than passed in,
    and then cross-checked against every subfamily name: a roman whose instances
    call themselves Italic, or an italic whose instances do not, is a naming bug
    that would otherwise ship as sixteen mislabelled files.
    """
    if "fvar" not in vf:
        raise InstanceError("not a variable font: no fvar table")
    italic = bool(vf["OS/2"].fsSelection & FS_ITALIC)
    name_table = vf["name"]
    instances: list[Instance] = []
    for entry in vf["fvar"].instances:
        subfamily = name_table.getDebugName(entry.subfamilyNameID)
        if not subfamily:
            raise InstanceError(
                f"fvar instance at {entry.coordinates} has no subfamily name "
                f"(name ID {entry.subfamilyNameID})"
            )
        missing = [axis for axis in PINNED_AXES if axis not in entry.coordinates]
        if missing:
            raise InstanceError(
                f"fvar instance {subfamily!r} has no {'/'.join(missing)} coordinate"
            )
        if entry.coordinates["opsz"] != STATIC_OPSZ:
            raise InstanceError(
                f"fvar instance {subfamily!r} sits at opsz "
                f"{entry.coordinates['opsz']:g}, not {STATIC_OPSZ}; the statics are "
                f"the text optical size only"
            )
        names_italic = subfamily == ITALIC_SUFFIX or subfamily.endswith(
            f" {ITALIC_SUFFIX}"
        )
        if names_italic != italic:
            raise InstanceError(
                f"fvar instance {subfamily!r} disagrees with the font's own "
                f"fsSelection ITALIC bit ({'set' if italic else 'clear'})"
            )
        instances.append(
            Instance(
                weight=int(entry.coordinates["wght"]),
                subfamily=subfamily,
                italic=italic,
            )
        )
    if not instances:
        raise InstanceError("the variable font has no fvar named instances")
    # One filename per instance: two that collide would silently ship fifteen.
    seen = Counter(instance.token for instance in instances)
    duplicates = sorted(token for token, count in seen.items() if count > 1)
    if duplicates:
        raise InstanceError(f"fvar names two instances the same: {', '.join(duplicates)}")
    return instances


# --------------------------------------------------------------------------- #
# Identity: names and bits
# --------------------------------------------------------------------------- #


def name_strings(family: Family, instance: Instance) -> dict[int, str]:
    """The name records a static carries, ID → string (§7.5).

    The RIBBI rule in full: a host that reads only IDs 1 and 2 can address four
    styles per family, so those four say ``Asterwell Text`` + one of
    Regular/Italic/Bold/Bold Italic and stop there. Every other weight needs its
    own legacy family — ``Asterwell Text SemiBold`` — with the italic half left
    in ID 2, and states the real family and subfamily in IDs 16 and 17 for hosts
    that read them.
    """
    postscript = f"{family.ps_family}-{instance.token}"
    full_name = f"{family.family} {instance.subfamily}"
    strings = {
        3: f"{family.version};{family.vendor_id};{postscript}",
        4: full_name,
        6: postscript,
    }
    if instance.ribbi:
        strings[1] = family.family
        strings[2] = instance.subfamily
    else:
        strings[1] = f"{family.family} {instance.weight_name}"
        strings[2] = ITALIC_SUFFIX if instance.italic else "Regular"
        strings[16] = family.family
        strings[17] = instance.subfamily
    return dict(sorted(strings.items()))


def apply_names(font: TTFont, family: Family, instance: Instance) -> dict[int, str]:
    """Write the static's identity over whatever the instancer inferred."""
    strings = name_strings(family, instance)
    name_table = font["name"]
    platform, encoding, language = assemble.WINDOWS_ENGLISH
    for name_id, string in strings.items():
        name_table.setName(string, name_id, platform, encoding, language)
    # A RIBBI style says everything IDs 16/17 would, and a host that finds them
    # both is entitled to believe the pair — so they come off rather than
    # sitting there agreeing.
    for name_id in TYPOGRAPHIC_NAME_IDS:
        if name_id not in strings:
            name_table.removeNames(nameID=name_id)
    for name_id in VARIABLE_ONLY_NAME_IDS:
        name_table.removeNames(nameID=name_id)
    return strings


def apply_style_bits(font: TTFont, instance: Instance) -> None:
    """Write the weight class and the style bits, leaving every other bit alone."""
    os2, head = font["OS/2"], font["head"]
    os2.usWeightClass = instance.weight
    os2.fsSelection = (
        os2.fsSelection & ~(FS_ITALIC | FS_BOLD | FS_REGULAR)
    ) | instance.fs_selection_bits
    head.macStyle = (head.macStyle & ~(MAC_BOLD | MAC_ITALIC)) | instance.mac_style_bits


# --------------------------------------------------------------------------- #
# The metrics freeze
# --------------------------------------------------------------------------- #


def frozen_differences(variable: Invariants, static: Invariants) -> list[str]:
    """§9.1 for a static: everything the variable font froze, minus what
    instancing legitimately changes — and it changes exactly four things.

    *The variation fields* (``fvar``, ``avar``, ``MVAR``) are read from tables a
    static does not have.

    *The style bits.* ``fsSelection`` is where BOLD, ITALIC and REGULAR live and
    this module has just written them, so the field is compared through a mask:
    every other bit — USE_TYPO_METRICS above all, which decides whose vertical
    metrics a host believes — must still be the variable font's.

    *The envelope.* ``head``'s bounding box and ``hhea.advanceWidthMax`` are
    recomputed on save from the glyphs that are actually there, and a variable
    font's are the *default instance's* — not the union over the design space.
    So they move in both directions and by design: measured on this family,
    ExtraLight comes out at (−353, −262, 1357, 1105) against the roman variable
    font's (−377, −268, 1443, 1136), and Bold at (−441, −311, 1589, 1191). Both
    are Literata's own drawing at those weights, not anything this build
    imported — §14.2's promise that no import can move the envelope is proved on
    the variable fonts, where the comparison is like for like. Freezing these
    two on a static could only be bought by writing numbers that contradict the
    outlines, and an ``advanceWidthMax`` wider than any advance in ``hmtx`` is a
    defect in its own right. They are therefore recorded in the report
    (:class:`StaticReport`) rather than asserted; checking them against the same
    location of *unmodified Literata* is a QA-time comparison (§9.1), not a
    build-time one, because it costs a second instancing run per file.

    *``rvrn``.* See :data:`VARIATION_FEATURES`. Every other GSUB and GPOS
    feature must survive: pinning a weight must not cost the family its small
    caps or its fractions.

    The vertical metrics — ``hhea`` ascent/descent/line gap, the ``OS/2`` typo
    and win pairs — and the units per em are compared as plain equalities, which
    is the part that matters most: two files of the same family that disagree
    about line height are two families.
    """
    ignored = {*VARIATION_FIELDS, *RESTYLED_FIELDS, *MEASURED_FIELDS, "gsub_features"}
    differences = [
        f"{name}: {getattr(variable, name)!r} → {getattr(static, name)!r}"
        for name in variable.__dataclass_fields__
        if name not in ignored and getattr(variable, name) != getattr(static, name)
    ]

    style_bits = FS_ITALIC | FS_BOLD | FS_REGULAR
    if (variable.fs_selection & ~style_bits) != (static.fs_selection & ~style_bits):
        differences.append(
            "fs_selection (outside the style bits): "
            f"{variable.fs_selection:#06x} → {static.fs_selection:#06x}"
        )

    lost = [
        tag
        for tag in variable.gsub_features
        if tag not in static.gsub_features and tag not in VARIATION_FEATURES
    ]
    gained = [tag for tag in static.gsub_features if tag not in variable.gsub_features]
    if lost or gained:
        differences.append(
            f"gsub_features: {variable.gsub_features!r} → {static.gsub_features!r}"
        )
    return differences


# --------------------------------------------------------------------------- #
# Building one static
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StaticReport:
    """What one instanced static is, for the console and for the caller."""

    style: str
    """``roman`` / ``italic`` — the variable font this was cut from."""

    instance: Instance
    output: Path
    glyphs: int
    head_bbox: tuple[int, int, int, int]
    """Recomputed on save from this instance's own outlines; reported rather
    than frozen — see :func:`frozen_differences`."""

    advance_width_max: int


def output_name(family: Family, instance: Instance) -> str:
    return f"{family.ps_family}-{instance.token}.ttf"


def build_static(
    vf_path: Path,
    instance: Instance,
    *,
    family: Family,
    frozen: Invariants,
    glyph_count: int,
    output: Path,
) -> StaticReport:
    """Instance one static, write it, and prove the metrics came through."""
    # Opened fresh for every instance — `instantiateVariableFont` consumes the
    # font it is given, and `inplace=True` says so rather than paying for a deep
    # copy of 2,297 glyphs eight times over. `open_source_font` also keeps
    # `head.modified` under SOURCE_DATE_EPOCH's control rather than the clock's.
    font = assemble.open_source_font(vf_path)
    try:
        static = instantiateVariableFont(
            font, instance.location, inplace=True, updateFontNames=True
        )
        apply_style_bits(static, instance)
        apply_names(static, family, instance)
        assemble.stamp_timestamp(static)
        output.parent.mkdir(parents=True, exist_ok=True)
        static.save(output)
    finally:
        font.close()

    # Measured on the bytes that ship, not on the object graph that wrote them.
    written = TTFont(output, recalcTimestamp=False)
    try:
        measured = assemble.invariants(written)
        drift = frozen_differences(frozen, measured)
        glyphs = len(written.getGlyphOrder())
        mac_records = [record for record in written["name"].names if record.platformID == 1]
    finally:
        written.close()
    if drift:
        raise InstanceError(
            f"{output.name}: instancing changed values it must not:\n  "
            + "\n  ".join(drift)
        )
    if mac_records:
        raise InstanceError(
            f"{output.name}: instancing put back {len(mac_records)} Macintosh name "
            f"records; the family carries Windows records only (D18)"
        )
    if glyphs != glyph_count:
        raise InstanceError(
            f"{output.name}: has {glyphs} glyphs, the variable font has "
            f"{glyph_count}; pinning an axis must not drop a glyph"
        )
    return StaticReport(
        style="italic" if instance.italic else "roman",
        instance=instance,
        output=output,
        glyphs=glyphs,
        head_bbox=measured.head_bbox,
        advance_width_max=measured.advance_width_max,
    )


def build_style_statics(
    vf_path: Path, *, family: Family, output_dir: Path, log: Log = print
) -> list[StaticReport]:
    """Cut every named instance of one variable font."""
    if not vf_path.is_file():
        raise InstanceError(f"{vf_path} is missing — run `mise run build`")
    variable = assemble.open_source_font(vf_path)
    try:
        frozen = assemble.invariants(variable)
        glyph_count = len(variable.getGlyphOrder())
        instances = named_instances(variable)
    finally:
        variable.close()

    reports = [
        build_static(
            vf_path,
            instance,
            family=family,
            frozen=frozen,
            glyph_count=glyph_count,
            output=output_dir / output_name(family, instance),
        )
        for instance in instances
    ]
    log(f"statics from {vf_path.name}: {len(reports)}")
    for report in reports:
        log(f"  {report.output.name:<32} {_describe(report)}")
    return reports


def _describe(report: StaticReport) -> str:
    instance = report.instance
    return (
        f"wght {instance.weight:<3} "
        f"fsSel {instance.fs_selection_bits:#04x} "
        f"mac {instance.mac_style_bits} "
        f"{'RIBBI ' if instance.ribbi else '16/17 '} "
        f"bbox {report.head_bbox} awm {report.advance_width_max}"
    )


def build_statics(
    variable: Sequence[StyleReport],
    *,
    family: Family,
    root: Path,
    log: Log = print,
) -> list[StaticReport]:
    """The static stage of ``asterwell-build build``: 8 instances per style."""
    output_dir = assemble.fonts_dir_for(root) / "ttf"
    reports: list[StaticReport] = []
    for style_report in variable:
        reports.extend(
            build_style_statics(
                style_report.output, family=family, output_dir=output_dir, log=log
            )
        )
    return reports
