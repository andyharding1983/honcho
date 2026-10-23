"""Sync local memory files (MEMORY.md / USER.md) into Honcho as change notes.

Local patch (replaces upstream's whole-file upload on every new session, plastic-labs/honcho#1278):
the last-synced lines of each file are kept in ``<memory_dir>/.honcho_memory_sync.json``. On a new
session only the lines added, changed or removed since then are sent, worded as plain sentences so
the deriver records removals as "no longer applies" rather than as live facts. The first run for a
file only records a baseline (its contents are assumed to be in Honcho already). MEMORY.md (the
agent's standing rules) goes to the assistant peer; USER.md (facts about the user) to the user peer.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path

from .session_auth import HonchoAuthError

logger = logging.getLogger("plugins.memory.honcho.session")

# (filename, peer kind, sentence introducing the change note) — peer kind picks user vs assistant peer.
# The note is sent as that peer's message, so the USER.md intro is first person (wording tested on the deriver).
_MEMORY_FILES = (
    ("MEMORY.md", "ai", "{ai} updated its own standing rules (its MEMORY.md file of rules {ai} follows)."),
    ("USER.md", "user", "My profile notes (USER.md, facts about me, {user}) have been updated."),
)
_STATE_FILE = ".honcho_memory_sync.json"
_ENTRY_SEPARATOR = "§"
_EDIT_SIMILARITY = 0.6  # below this, a replaced line is reported as removed plus added


def memory_lines(content: str) -> list[str]:
    """Non-empty lines of a memory file, each prefixed with its ``## `` section heading so a
    bullet still makes sense on its own; entry separators and headings themselves are dropped."""
    section, lines = "", []
    for raw in content.splitlines():
        line = raw.strip()
        if line == _ENTRY_SEPARATOR:
            section = ""  # a heading only covers its own entry
            continue
        if not line:
            continue
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        line = line[2:].strip() if line.startswith(("- ", "* ")) else line
        lines.append(f"{section}: {line}" if section else line)
    return lines


def diff_lines(old: list[str], new: list[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """(added, changed as (old, new) pairs, removed) between two line lists."""
    added: list[str] = []
    changed: list[tuple[str, str]] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "insert":
            added += new[j1:j2]
        elif tag == "delete":
            removed += old[i1:i2]
        elif tag == "replace":
            # Pair each old line with its most similar new line; dissimilar lines are a separate
            # removal and addition, not an edit.
            remaining = list(new[j1:j2])
            for line in old[i1:i2]:
                best = max(remaining, default=None,
                           key=lambda n: difflib.SequenceMatcher(a=line, b=n, autojunk=False).ratio())
                if best is not None and difflib.SequenceMatcher(a=line, b=best, autojunk=False).ratio() >= _EDIT_SIMILARITY:
                    changed.append((line, best))
                    remaining.remove(best)
                else:
                    removed.append(line)
            added += remaining
    return added, changed, removed


def change_note(intro: str, added: list[str], changed: list[tuple[str, str]], removed: list[str]) -> str:
    parts = [intro]
    if added:
        parts.append("Added:\n" + "\n".join(f"- {line}" for line in added))
    if changed:
        parts.append("Changed (the old wording is replaced and no longer applies):\n" + "\n".join(
            f'- Was: "{old}"\n  Now: "{new}"' for old, new in changed))
    if removed:
        parts.append("Deleted from the file (these notes are outdated and should be forgotten; deleting a note does "
                     "NOT mean its opposite is now true):\n" + "\n".join(
            f"- {line}" for line in removed))
    return "<memory_file_update>\n" + "\n\n".join(parts) + "\n</memory_file_update>"


class SessionMigrationMixin:
    def migrate_memory_files(self, session_key: str, memory_dir: str) -> bool:
        """Send changes in MEMORY.md / USER.md since the last sync; the first run for a file only
        records a baseline. True if at least one change note was sent."""
        memory_path = Path(memory_dir)
        if not memory_path.exists():
            return False

        session = self._cached_session(session_key)
        if not session:
            logger.warning("No local session cached for '%s', skipping memory sync", session_key)
            return False
        if session.honcho_session_id not in self._sessions_cache:
            logger.warning("No Honcho session cached for '%s', skipping memory sync", session_key)
            return False

        # Owner-scoped: these files describe the install owner; uploading them under another
        # human's peer would make Honcho attribute the owner's facts to that person. The owner is
        # the CONFIG peerName, never a re-resolution of the session's own peer (that would compare
        # the triggering user to themselves). No declared owner: nobody can be proven to be the owner.
        owner_peer_id = self._declared_owner_peer_id()
        if owner_peer_id is None or session.user_peer_id != owner_peer_id:
            logger.info("Skipping memory-file sync: session user peer '%s' is not the declared owner (peerName=%s)",
                        session.user_peer_id, owner_peer_id or "unset")
            return False

        # One sync at a time across processes (gateway and webui can start sessions together).
        state_path = memory_path / _STATE_FILE
        with open(memory_path / (_STATE_FILE + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {}
            except Exception as e:
                logger.error("Unreadable %s, skipping memory sync: %s", state_path, e)
                return False
            # Keyed by workspace and peers, so another workspace or peer gets its own baseline.
            workspace = getattr(self._config, "workspace_id", "") if self._config is not None else ""
            target = f"{workspace or ''}|{session.user_peer_id}|{session.assistant_peer_id}"
            synced = state.setdefault(target, {})

            sent = dirty = False
            for filename, target_kind, intro in _MEMORY_FILES:
                filepath = memory_path / filename
                lines = memory_lines(filepath.read_text(encoding="utf-8")) if filepath.exists() else []
                digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
                previous = synced.get(filename)
                if previous is None:
                    synced[filename] = {"sha256": digest, "lines": lines}
                    dirty = True
                    logger.info("Recorded %s baseline for Honcho memory sync (%d lines, nothing sent)",
                                filename, len(lines))
                    continue
                if previous.get("sha256") == digest:
                    continue
                added, changed, removed = diff_lines(previous.get("lines", []), lines)
                if not (added or changed or removed):
                    synced[filename] = {"sha256": digest, "lines": lines}
                    dirty = True
                    continue
                target_peer_id = session.user_peer_id if target_kind == "user" else session.assistant_peer_id
                note = change_note(intro.format(ai=session.assistant_peer_id, user=session.user_peer_id),
                                   added, changed, removed)

                def _send(note: str = note, target_peer_id: str = target_peer_id) -> None:
                    peer = self._get_or_create_peer(target_peer_id)
                    self._sdk_session(session.honcho_session_id).add_messages([peer.message(
                        note, metadata={"source": "local_memory_sync", "original_file": filename})])

                try:
                    self._authed_call("memory file sync", _send)
                except HonchoAuthError:
                    logger.error("Honcho memory sync stopped at %s: auth failed", filename)
                    break
                except Exception as e:
                    logger.error("Failed to send %s changes to Honcho: %s", filename, e)
                    continue
                logger.info("Sent %s changes to Honcho for %s (%s peer: +%d ~%d -%d)", filename, session_key,
                            target_kind, len(added), len(changed), len(removed))
                synced[filename] = {"sha256": digest, "lines": lines}
                sent = dirty = True

            if dirty:
                tmp = state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, state_path)
        return sent
