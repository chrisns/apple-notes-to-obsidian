#!/usr/bin/env python3
"""Convert Apple Notes into Obsidian markdown, one-way.

Modes
-----

Single note:
    migrate.py <note-id> <out-dir> [--vault <vault-root>]

Sync all notes:
    migrate.py --sync <out-dir> [--vault <vault-root>] [--limit N] [--force]

In `--sync` mode the script walks every Apple Note, looks up an existing
markdown file by `source-id` frontmatter, and re-migrates only when the
note's `modified` timestamp has advanced since the previous migration. With
`--force` every note is rewritten regardless. `--limit N` processes the most
recently modified N notes (handy for dry runs).

Drawings / paper bundles are decoded into vector tldraw `draw` shapes inside
Ink `.writing` files; pasted images are extracted as files alongside the
markdown. See `_migration/decode_paper.swift` for the Coherence/PaperKit
bridge that makes Paper bundle decoding possible.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from ink_writer import (
    ink_embed_block,
    write_strokes_writing_file,
    write_writing_file,
)

NOTES_DIR = Path.home() / "Library/Group Containers/group.com.apple.notes"
NOTES_DB = NOTES_DIR / "NoteStore.sqlite"

SCRIPT_DIR = Path(__file__).resolve().parent
DUMP_AS = SCRIPT_DIR / "dump_note.applescript"
PKDRAWING_BIN = SCRIPT_DIR / "decode_pkdrawing"
PAPER_BIN = SCRIPT_DIR / "decode_paper"


# --- slug + YAML helpers ----------------------------------------------------

_SLUG_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SLUG_WS = re.compile(r"\s+")


def slugify(name: str, fallback: str) -> str:
    s = _SLUG_BAD.sub("", name).strip()
    s = _SLUG_WS.sub(" ", s)
    if not s or s.strip(".") == "":
        s = fallback
    return s[:120]


def _stamp_paths(paths: list[Path], created: str, modified: str) -> None:
    """Make filesystem birthtime + mtime match the source note.

    Obsidian's "Files and links" view, the file explorer, and most plugins
    (Dataview, etc.) read filesystem dates rather than frontmatter. We set
    both so the metadata is consistent with what Apple Notes recorded.
    """
    try:
        m_ts = datetime.datetime.fromisoformat(modified).timestamp() if modified else None
    except ValueError:
        m_ts = None
    try:
        c_ts = datetime.datetime.fromisoformat(created) if created else None
    except ValueError:
        c_ts = None

    for path in paths:
        if not path.exists():
            continue
        if m_ts is not None:
            try:
                os.utime(path, (m_ts, m_ts))
            except OSError:
                pass
        if c_ts is not None:
            # macOS-only `SetFile -d` updates the HFS/APFS creation date.
            stamp = c_ts.strftime("%m/%d/%Y %H:%M:%S")
            subprocess.run(
                ["SetFile", "-d", stamp, str(path)],
                check=False, capture_output=True,
            )


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_block_scalar(s: str, indent: int = 2) -> str:
    pad = " " * indent
    return "|\n" + "\n".join(pad + line for line in s.splitlines())


# --- SQLite -----------------------------------------------------------------


def _connect_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{NOTES_DB}?mode=ro", uri=True)


def _discover_note_id_prefix() -> str:
    """Apple Notes' AppleScript ids are scoped to a per-machine Core Data store
    UUID, so the prefix isn't portable. Ask AppleScript for any note's id once
    and snip the prefix off it. Cached.
    """
    if hasattr(_discover_note_id_prefix, "_cache"):
        return _discover_note_id_prefix._cache  # type: ignore[attr-defined]
    out = subprocess.run(
        ["osascript", "-e",
         'tell application "Notes" to return id of first note'],
        check=True, capture_output=True,
    ).stdout.decode("utf-8", "replace").strip()
    if "/ICNote/p" not in out:
        raise RuntimeError(
            f"Couldn't discover the Notes store id from AppleScript: {out!r}"
        )
    prefix = out.split("/ICNote/")[0] + "/ICNote/p"
    _discover_note_id_prefix._cache = prefix  # type: ignore[attr-defined]
    return prefix


def _note_pk_from_id(note_id: str) -> int:
    return int(note_id.rsplit("/", 1)[-1].lstrip("p"))


def fetch_note_metadata(note_pk: int) -> dict:
    """Pull SQLite-only fields not exposed by AppleScript.

    Apple's OCR/handwriting-recognition results live on attachment rows
    (Z_ENT=5), not the note's main row. Aggregate them so the note's
    frontmatter has the full searchable text. The account FK has migrated
    across columns over time — fall back across all known variants.
    """
    conn = _connect_ro()
    try:
        note_row = conn.execute(
            """
            SELECT COALESCE(ZACCOUNT, ZACCOUNT1, ZACCOUNT2, ZACCOUNT3,
                            ZACCOUNT4, ZACCOUNT5, ZACCOUNT6, ZACCOUNT7,
                            ZACCOUNT8)
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE Z_PK = ?
            """,
            (note_pk,),
        ).fetchone()
        account_pk = note_row[0] if note_row else None

        ocr_rows = conn.execute(
            """
            SELECT ZHANDWRITINGSUMMARY, ZOCRSUMMARY
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE (ZNOTE = ? OR ZNOTE1 = ?)
            """,
            (note_pk, note_pk),
        ).fetchall()
    finally:
        conn.close()

    handwriting_parts: list[str] = []
    ocr_parts: list[str] = []
    for hw, ocr in ocr_rows:
        if hw:
            handwriting_parts.append(hw)
        if ocr:
            ocr_parts.append(ocr)

    return {
        "handwriting_ocr": "\n\n".join(handwriting_parts),
        "image_ocr": "\n\n".join(ocr_parts),
        "account_pk": account_pk,
    }


def fetch_checklist_paragraphs(note_pk: int) -> list[dict]:
    """Return all checklist paragraphs as `[{"text": str, "checked": bool}, ...]`.

    Apple Notes' AppleScript HTML body strips checkbox state — checklist items
    come out as plain `<ul><li>` bullets. The state lives in the gzipped
    protobuf at `ZICNOTEDATA.ZDATA`. We walk the protobuf with a generic
    decoder (no .proto schema needed): style_type 103 marks a checklist run,
    and the per-paragraph todo's field 2 (0/1) is the checked flag.
    """
    import gzip
    conn = _connect_ro()
    try:
        row = conn.execute(
            "SELECT ZDATA FROM ZICNOTEDATA "
            "WHERE Z_PK = (SELECT ZNOTEDATA FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ?)",
            (note_pk,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return []
    try:
        raw = gzip.decompress(row[0])
    except Exception:
        return []

    def _read_varint(buf: bytes, p: int) -> tuple[int, int]:
        v = 0; s = 0
        while True:
            x = buf[p]; p += 1
            v |= (x & 0x7f) << s
            if not (x & 0x80):
                break
            s += 7
        return v, p

    def _parse_pb(buf: bytes) -> dict:
        out: dict[int, list] = {}
        p = 0
        while p < len(buf):
            try:
                tag, p = _read_varint(buf, p)
            except IndexError:
                break
            field = tag >> 3
            wire = tag & 7
            if wire == 0:
                v, p = _read_varint(buf, p)
                out.setdefault(field, []).append(v)
            elif wire == 2:
                length, p = _read_varint(buf, p)
                out.setdefault(field, []).append(buf[p:p + length])
                p += length
            elif wire == 5:
                p += 4
            elif wire == 1:
                p += 8
            else:
                break
        return out

    try:
        outer = _parse_pb(raw)
        inner = _parse_pb(outer[2][0])
        doc = _parse_pb(inner[3][0])
        text = doc[2][0].decode("utf-8", "replace")
        runs = doc.get(5, [])
    except (KeyError, IndexError, UnicodeDecodeError):
        return []

    # Per-character mask of (style_type, checked).
    mask: list[tuple[int, bool]] = [(0, False)] * len(text)
    cursor = 0
    for run_raw in runs:
        run = _parse_pb(run_raw)
        length = run.get(1, [0])[0]
        style = _parse_pb(run[2][0]) if 2 in run else {}
        style_type = style.get(1, [0])[0]
        checked = False
        if style_type == 103 and 5 in style:
            todo = _parse_pb(style[5][0])
            checked = bool(todo.get(2, [0])[0])
        end = min(cursor + length, len(text))
        for i in range(cursor, end):
            mask[i] = (style_type, checked)
        cursor = end

    # Walk paragraphs (`\n`-delimited).
    items: list[dict] = []
    pos = 0
    for line in text.split("\n"):
        if line.strip() and pos < len(mask):
            style_type, checked = mask[pos]
            if style_type == 103:
                items.append({"text": line, "checked": checked})
        pos += len(line) + 1
    return items


def fetch_account_uuid(account_pk: int | None) -> str | None:
    """Resolve the account FK to its on-disk folder UUID, with a single-account
    fallback when the FK is null (very recent notes occasionally are)."""
    conn = _connect_ro()
    try:
        if account_pk:
            row = conn.execute(
                "SELECT ZIDENTIFIER FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ?",
                (account_pk,),
            ).fetchone()
            if row and row[0]:
                return row[0]
        accounts_dir = NOTES_DIR / "Accounts"
        if accounts_dir.is_dir():
            uuid_dirs = [d.name for d in accounts_dir.iterdir() if d.is_dir()]
            if len(uuid_dirs) == 1:
                return uuid_dirs[0]
    finally:
        conn.close()
    return None


_IMAGE_UTIS = (
    "com.apple.drawing.2",
    "com.apple.paper",
    "public.png",
    "public.jpeg",
    "com.compuserve.gif",
    "public.tiff",
)


def fetch_attachments(note_pk: int) -> list[dict]:
    """All image-shaped attachments on a note, in (likely) document order.

    Apple Notes' newer "Paper" feature lives alongside the legacy
    `com.apple.drawing.2` format. When both exist on the same note, they
    refer to the same drawing — Apple keeps the legacy as a fallback. The
    UI only shows the Paper version, so we drop the drawing.2 to avoid
    emitting it twice.
    """
    conn = _connect_ro()
    try:
        cursor = conn.execute(
            f"""
            SELECT Z_PK, ZTYPEUTI, ZIDENTIFIER, ZMERGEABLEDATA1
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE (ZNOTE = ? OR ZNOTE1 = ?)
              AND ZTYPEUTI IN ({",".join("?" * len(_IMAGE_UTIS))})
            ORDER BY Z_PK
            """,
            (note_pk, note_pk, *_IMAGE_UTIS),
        )
        rows = [
            {"pk": pk, "uti": uti, "identifier": identifier, "blob": blob}
            for pk, uti, identifier, blob in cursor.fetchall()
        ]
    finally:
        conn.close()

    has_paper = any(r["uti"] == "com.apple.paper" for r in rows)
    if has_paper:
        rows = [r for r in rows if r["uti"] != "com.apple.drawing.2"]
    return rows


def list_all_notes() -> list[dict]:
    """All real notes (not attachments), ordered by recency.

    Apple Notes has migrated the modification-date column over time; on the
    user's database `ZMODIFICATIONDATE` is null for ICNote rows and the real
    value lives on `ZMODIFICATIONDATE1`. Coalesce so newer or older databases
    work either way.
    """
    conn = _connect_ro()
    try:
        rows = conn.execute(
            """
            SELECT Z_PK, ZTITLE1,
                   COALESCE(ZMODIFICATIONDATE1, ZMODIFICATIONDATE) AS m
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE Z_ENT = 12
              AND ZNOTEDATA IS NOT NULL
              AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION = 0)
              AND ZTITLE1 IS NOT NULL
            ORDER BY m DESC
            """,
        ).fetchall()
    finally:
        conn.close()
    prefix = _discover_note_id_prefix()
    return [
        {
            "pk": pk,
            "id": f"{prefix}{pk}",
            "title": title,
            "modified": modified or 0.0,
        }
        for pk, title, modified in rows
    ]


# --- decoders ---------------------------------------------------------------


def _run_decoder(binary: Path, arg: str) -> dict:
    proc = subprocess.run([str(binary), arg], check=True, capture_output=True)
    return json.loads(proc.stdout)


def decode_pkdrawing_blob(blob: bytes) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(blob)
        tmp = f.name
    try:
        return _run_decoder(PKDRAWING_BIN, tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def decode_paper_bundle(account_uuid: str, attachment_uuid: str) -> dict:
    bundle_path = (
        NOTES_DIR / "Accounts" / account_uuid / "Paper" / "Bundles"
        / f"{attachment_uuid}.bundle"
    )
    if not bundle_path.is_dir():
        raise FileNotFoundError(f"paper bundle not found: {bundle_path}")
    return _run_decoder(PAPER_BIN, str(bundle_path))


# --- AppleScript body dump --------------------------------------------------


def dump_note(note_id: str) -> dict:
    out = subprocess.run(
        ["osascript", str(DUMP_AS), note_id],
        check=True,
        capture_output=True,
    ).stdout
    if out.endswith(b"\n"):
        out = out[:-1]
    parts = out.split(b"\x00")
    if len(parts) != 6:
        raise RuntimeError(
            f"Expected 6 NUL-separated fields, got {len(parts)} for note {note_id}"
        )
    return {
        "id": parts[0].decode("utf-8", "replace"),
        "name": parts[1].decode("utf-8", "replace"),
        "created": parts[2].decode("utf-8", "replace"),
        "modified": parts[3].decode("utf-8", "replace"),
        "folder": parts[4].decode("utf-8", "replace"),
        "body_html": parts[5].decode("utf-8", "replace"),
    }


# --- attachment dispatch ----------------------------------------------------

_DATA_IMG = re.compile(
    r'<img[^>]*src="data:image/(?P<ext>png|jpeg|jpg|gif|tiff);base64,(?P<data>[^"]+)"[^>]*>',
    re.IGNORECASE,
)
_SENTINEL = "@@APPLENOTE_ATT_{}@@"


def _ext_normalised(ext: str) -> str:
    return "jpg" if ext in ("jpeg", "jpg") else ext


def _emit_drawing(
    att: dict,
    vault_root: Path,
    note_slug: str,
    idx: int,
    digest: str | None,
    account_uuid: str | None,
) -> str | None:
    """Decode a drawing attachment to a .writing file and return the embed block.
    Returns None on failure."""
    digest = digest or (att.get("identifier") or "0000")[:8].lower()
    ink_path = f"Ink/Writing/{note_slug} - drawing-{idx:02d}-{digest}.writing"

    if att["uti"] == "com.apple.drawing.2" and att.get("blob"):
        try:
            decoded = decode_pkdrawing_blob(att["blob"])
        except Exception as e:
            print(f"  drawing.2 decode failed: {e}", file=sys.stderr)
            return None
        write_strokes_writing_file(
            decoded["strokes"], decoded["bounds"],
            vault_root / ink_path,
            source_name=f"{note_slug}-{idx:02d}",
        )
        return "\n" + ink_embed_block(ink_path) + "\n"

    if att["uti"] == "com.apple.paper" and account_uuid and att.get("identifier"):
        try:
            decoded = decode_paper_bundle(account_uuid, att["identifier"])
        except Exception as e:
            print(
                f"  paper decode failed for {att['identifier']}: {e}",
                file=sys.stderr,
            )
            return None
        write_strokes_writing_file(
            decoded["strokes"], decoded["bounds"],
            vault_root / ink_path,
            source_name=f"{note_slug}-{idx:02d}",
        )
        return "\n" + ink_embed_block(ink_path) + "\n"

    return None


def extract_inline_attachments(
    html: str,
    vault_root: Path,
    out_dir: Path,
    note_slug: str,
    attachments: list[dict],
    account_uuid: str | None,
) -> tuple[str, list[str], list[str], list[Path]]:
    """Replace each inline data: image with a sentinel and emit assets.

    Returns (rewritten_html, sentinel_replacements, trailing_blocks, asset_paths).
    `asset_paths` is every file we wrote (drawings + images) so the caller can
    stamp them with the source note's filesystem dates.
    """
    replacements: list[str] = []
    asset_paths: list[Path] = []
    counter = {"n": 0}
    queue = list(attachments)
    consumed_pks: set[int] = set()

    def _ink_target(idx: int, digest: str) -> Path:
        return vault_root / f"Ink/Writing/{note_slug} - drawing-{idx:02d}-{digest}.writing"

    def repl(m: re.Match) -> str:
        ext = m.group("ext").lower()
        raw = base64.b64decode(m.group("data"))
        counter["n"] += 1
        idx = counter["n"]
        digest = hashlib.sha1(raw).hexdigest()[:8]
        att = queue.pop(0) if queue else None
        uti = (att or {}).get("uti")

        if att and uti in ("com.apple.drawing.2", "com.apple.paper"):
            block = _emit_drawing(
                att, vault_root, note_slug, idx, digest, account_uuid,
            )
            if block is not None:
                consumed_pks.add(att["pk"])
                asset_paths.append(_ink_target(idx, digest))
                replacements.append(block)
                return _SENTINEL.format(idx)

        # Photo / scan / fallback (or drawing decode failed).
        out_ext = _ext_normalised(ext)
        rel_path = f"_attachments/{note_slug}/{idx:02d}-{digest}.{out_ext}"
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        asset_paths.append(target)
        replacements.append(f"![{Path(rel_path).stem}]({rel_path})")
        if att:
            consumed_pks.add(att["pk"])
        return _SENTINEL.format(idx)

    rewritten = _DATA_IMG.sub(repl, html)

    # Drawings/paper bundles that AppleScript's body getter omitted: render
    # them at the end. Index continues after the inline ones so .writing
    # filenames remain unique.
    trailing: list[str] = []
    for att in attachments:
        if att["pk"] in consumed_pks:
            continue
        if att["uti"] not in ("com.apple.drawing.2", "com.apple.paper"):
            continue
        counter["n"] += 1
        digest = (att.get("identifier") or "0000")[:8].lower()
        block = _emit_drawing(
            att, vault_root, note_slug, counter["n"], digest, account_uuid,
        )
        if block:
            asset_paths.append(_ink_target(counter["n"], digest))
            trailing.append(block)

    return rewritten, replacements, trailing, asset_paths


def replace_sentinels(body: str, replacements: list[str]) -> str:
    for i, replacement in enumerate(replacements, start=1):
        body = body.replace(_SENTINEL.format(i), replacement)
    return body


# --- HTML → markdown --------------------------------------------------------


def html_to_md(html: str) -> str:
    proc = subprocess.run(
        ["pandoc", "--from=html", "--to=gfm-raw_html", "--wrap=none"],
        input=html.encode("utf-8"),
        check=True,
        capture_output=True,
    )
    return proc.stdout.decode("utf-8")


_BULLET_RE = re.compile(r"^(\s*)(?:-|\*)\s+(?P<rest>.*)$")
_STRIKE_WRAP_RE = re.compile(r"^~~(?P<inner>.*)~~$")


def apply_checkboxes(md_body: str, items: list[dict]) -> str:
    """Convert plain bullet items to GFM task-list items where they match a
    checklist paragraph from the source note's protobuf.

    Pandoc renders Apple Notes' "checked" items as ``~~strike~~`` markdown
    (Apple Notes' rendered checked state has strikethrough); we strip that
    wrapper because the checkbox already conveys the same meaning.
    """
    if not items:
        return md_body
    # Build a normalised text → checked map. Many items can repeat the same
    # text; track all occurrences and consume in order.
    pending: dict[str, list[bool]] = {}
    for it in items:
        key = it["text"].strip()
        pending.setdefault(key, []).append(it["checked"])

    def convert(line: str) -> str:
        m = _BULLET_RE.match(line)
        if not m:
            return line
        rest = m.group("rest").strip()
        # Pandoc may have wrapped checked items in ~~...~~. Match either form.
        stripped = rest
        was_strike = False
        sm = _STRIKE_WRAP_RE.match(rest)
        if sm:
            stripped = sm.group("inner").strip()
            was_strike = True
        # Look up: try the un-wrapped text first, then the original.
        for key in (stripped, rest):
            if key in pending and pending[key]:
                checked = pending[key].pop(0)
                mark = "x" if checked else " "
                return f"{m.group(1)}- [{mark}] {stripped}"
        # No matching checklist item — leave bullet alone, but if pandoc
        # gave us a struck-through bullet from a checked item we couldn't
        # resolve, still convert (best effort).
        if was_strike:
            return f"{m.group(1)}- [x] {stripped}"
        return line

    return "\n".join(convert(ln) for ln in md_body.split("\n"))


def clean_md_body(md_body: str, title: str) -> str:
    # Apple Notes echoes the title at the top of the body. Strip it.
    title_re = re.escape(title)
    md_body = re.sub(
        rf"^\s*(?:#\s+{title_re}|{title_re}\n=+|{title_re})\s*\n+",
        "", md_body, count=1,
    )
    # Pandoc surfaces Apple Notes' <br> as standalone "\" lines.
    md_body = re.sub(r"^(?:\s*\\\s*\n)+", "", md_body)
    md_body = re.sub(r"(?:\\\s*\n){2,}", "\n", md_body)
    md_body = re.sub(r"^([-*])\s*\\?\s*$\n?", "", md_body, flags=re.MULTILINE)
    md_body = re.sub(r"\\(?=\n)", "", md_body)
    return md_body.rstrip() + "\n"


# --- frontmatter ------------------------------------------------------------


def build_frontmatter(meta: dict, extras: dict) -> str:
    lines = ["---"]
    lines.append("source: apple-notes")
    lines.append(f"source-id: {yaml_quote(meta['id'])}")
    if meta["folder"]:
        lines.append(f"original-folder: {yaml_quote(meta['folder'])}")
    lines.append(f"created: {meta['created']}")
    lines.append(f"modified: {meta['modified']}")
    if extras.get("handwriting_ocr"):
        lines.append(
            f"handwriting-ocr: {yaml_block_scalar(extras['handwriting_ocr'])}"
        )
    if extras.get("image_ocr"):
        lines.append(f"image-ocr: {yaml_block_scalar(extras['image_ocr'])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# --- existing-file index for sync mode --------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_KV_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Lightweight YAML scan good enough for the keys we emit ourselves."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line.startswith(" "):
            # nested list item or block scalar continuation; not interested.
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key, raw_val = kv.group(1), kv.group(2)
        if raw_val.startswith('"') and raw_val.endswith('"'):
            raw_val = raw_val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key] = raw_val
    return out


_INDEX_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "_migration"}


def build_existing_index(vault: Path) -> dict[str, dict]:
    """Walk the *entire vault* and return ``{source-id: {path, modified, slug}}``
    for every markdown file with ``source: apple-notes`` in its frontmatter.

    The index isn't bound to the migration's output directory: if you move a
    migrated note from `Notes/Inbox/foo.md` to `Notes/Projects/foo.md`, sync
    finds it by `source-id` in its new location and keeps updating it there.
    """
    index: dict[str, dict] = {}
    if not vault.is_dir():
        return index
    for root, dirs, files in os.walk(vault):
        # Prune obvious-never-content folders early so a 1000-note vault stays
        # cheap to walk.
        dirs[:] = [d for d in dirs if d not in _INDEX_SKIP_DIRS]
        root_path = Path(root)
        for name in files:
            if not name.endswith(".md"):
                continue
            md_path = root_path / name
            try:
                head = md_path.read_text(encoding="utf-8", errors="replace")[:8192]
            except OSError:
                continue
            fm = _parse_frontmatter(head)
            if fm.get("source") != "apple-notes":
                continue
            sid = fm.get("source-id")
            if not sid:
                continue
            index[sid] = {
                "path": md_path,
                "modified": fm.get("modified", ""),
                "slug": md_path.stem,
            }
    return index


# --- per-note migration -----------------------------------------------------


def migrate_one(
    note_id: str,
    out_dir: Path,
    vault: Path,
    *,
    existing_index: dict[str, dict] | None = None,
    force: bool = False,
) -> tuple[str, Path | None]:
    """Migrate one note. Returns (status, target_path).

    status: "wrote" | "renamed" | "skipped" | "failed"
    """
    try:
        meta = dump_note(note_id)
    except Exception as e:
        print(f"  dump failed for {note_id}: {e}", file=sys.stderr)
        return "failed", None

    if not force and existing_index is not None:
        prev = existing_index.get(meta["id"])
        if prev and prev.get("modified") and prev["modified"] >= meta["modified"]:
            # Body is up-to-date; still re-stamp filesystem dates so any drift
            # from older migrations or manual edits gets corrected. Cheap and
            # idempotent.
            prev_path = prev["path"] if isinstance(prev["path"], Path) else Path(prev["path"])
            _stamp_paths([prev_path], meta["created"], meta["modified"])
            return "skipped", prev_path

    pk = _note_pk_from_id(note_id)
    extras = fetch_note_metadata(pk)
    account_uuid = fetch_account_uuid(extras.get("account_pk"))

    fallback = note_id.rsplit("/", 1)[-1]
    slug = slugify(meta["name"], fallback)

    attachments = fetch_attachments(pk)
    rewritten, replacements, trailing, asset_paths = extract_inline_attachments(
        meta["body_html"], vault, out_dir, slug, attachments, account_uuid,
    )
    md_body = html_to_md(rewritten)
    md_body = replace_sentinels(md_body, replacements)
    checklist_items = fetch_checklist_paragraphs(pk)
    md_body = apply_checkboxes(md_body, checklist_items)
    md_body = clean_md_body(md_body, meta["name"])
    if trailing:
        md_body = md_body.rstrip() + "\n" + "\n".join(trailing) + "\n"

    frontmatter = build_frontmatter(meta, extras)

    prev_path: Path | None = None
    if existing_index is not None:
        prev = existing_index.get(meta["id"])
        if prev:
            prev_path = Path(prev["path"]) if not isinstance(prev["path"], Path) else prev["path"]

    # Where to write: stay in the file's existing folder if we know one (so
    # any user-driven reorganisation survives sync). Net-new notes default to
    # `out_dir`.
    target_dir = prev_path.parent if prev_path else out_dir
    target = target_dir / f"{slug}.md"

    if prev_path and prev_path != target:
        # Note was renamed in Apple Notes — drop the old filename (the new
        # write below replaces it in the same directory).
        prev_path.unlink(missing_ok=True)

    if target.exists() and (not prev_path or prev_path != target):
        # Filename collision with an unrelated note in the same folder.
        existing_text = target.read_text(encoding="utf-8", errors="replace")
        if f"source-id: {yaml_quote(meta['id'])}" not in existing_text:
            short = note_id.rsplit("/", 1)[-1]
            target = target_dir / f"{slug} ({short}).md"

    target.write_text(frontmatter + md_body, encoding="utf-8")
    _stamp_paths([target, *asset_paths], meta["created"], meta["modified"])

    status = "renamed" if (prev_path and prev_path != target) else "wrote"
    return status, target


# --- CLI --------------------------------------------------------------------


def _resolve_vault(out_dir: Path, vault: Path | None) -> Path:
    if vault is not None:
        return vault
    probe = out_dir
    for _ in range(8):
        if (probe / ".obsidian").is_dir():
            return probe
        if probe.parent == probe:
            break
        probe = probe.parent
    return out_dir.parent


def _run_sync(
    out_dir: Path,
    vault: Path,
    *,
    limit: int | None,
    force: bool,
) -> int:
    notes = list_all_notes()
    if limit is not None:
        notes = notes[:limit]
    print(f"sync: {len(notes)} note(s) to consider")
    existing = build_existing_index(vault)

    counts = {"wrote": 0, "renamed": 0, "skipped": 0, "failed": 0}
    for n in notes:
        status, path = migrate_one(
            n["id"], out_dir, vault,
            existing_index=existing, force=force,
        )
        counts[status] = counts.get(status, 0) + 1
        if status == "skipped":
            continue
        try:
            rel = path.relative_to(vault) if path else "(none)"
        except ValueError:
            rel = path
        symbol = {"wrote": "+", "renamed": "↪", "failed": "!"}.get(status, "?")
        title = (n.get("title") or "").replace("\n", " ")[:60]
        print(f"  {symbol} {rel}  ({title})")

    print(
        f"done: wrote={counts['wrote']} renamed={counts['renamed']} "
        f"skipped={counts['skipped']} failed={counts['failed']}"
    )
    return 0 if counts.get("failed", 0) == 0 else 1


def main() -> int:
    args = sys.argv[1:]
    vault: Path | None = None
    limit: int | None = None
    force = False

    if "--vault" in args:
        i = args.index("--vault")
        vault = Path(args[i + 1]).resolve()
        del args[i : i + 2]
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        del args[i : i + 2]
    if "--force" in args:
        force = True
        args.remove("--force")

    if args and args[0] == "--sync":
        if len(args) != 2:
            print(__doc__, file=sys.stderr)
            return 2
        out_dir = Path(args[1]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        vault = _resolve_vault(out_dir, vault)
        return _run_sync(out_dir, vault, limit=limit, force=force)

    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    note_id, out_arg = args
    out_dir = Path(out_arg).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    vault = _resolve_vault(out_dir, vault)

    existing = build_existing_index(vault)
    status, target = migrate_one(
        note_id, out_dir, vault,
        existing_index=existing, force=force,
    )
    if target is None:
        return 1
    try:
        rel = target.relative_to(vault)
    except ValueError:
        rel = target
    symbol = {"wrote": "wrote", "renamed": "renamed", "skipped": "skipped"}.get(status, status)
    print(f"{symbol} {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
