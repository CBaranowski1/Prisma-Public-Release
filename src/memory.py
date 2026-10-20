
import json
import logging
import os
import threading
import time
import uuid
import re
from typing import Any, Callable, List, Dict, Optional, Tuple
from datetime import datetime

from core.constants import (
    MAX_MEMORY_ENTRIES,
    LEXICAL_FALLBACK_SIMILARITY_THRESHOLD,
    IDENTITY_MEMORY_INJECTION_CAP,
)

logger = logging.getLogger(__name__)


def prune_memory(entries: List[Dict], keep: int = MAX_MEMORY_ENTRIES) -> List[Dict]:
    """Bounded, pinned-safe eviction (GRAY-BRN-06): keep at most `keep`
    entries, dropping the OLDEST UNPINNED entries first (by `timestamp`).

    Pinned entries (`entry.get("pinned")` truthy) are NEVER placed in the
    eviction pool -- even if that means the returned list still exceeds
    `keep` (30-RESEARCH.md Pitfall 5: a naive "keep N most recent" policy
    would delete a user's identity/pinned facts once the cap is crossed).

    Pure function -- never raises, never touches disk. Returns the input
    list unchanged (same objects) when already within the cap. Modeled on
    src/tool_output_store.py::_prune_store's oldest-first eviction shape.
    """
    if len(entries) <= keep:
        return entries

    pinned = [e for e in entries if e.get("pinned")]
    unpinned = sorted(
        (e for e in entries if not e.get("pinned")),
        key=lambda e: e.get("timestamp", 0),
    )
    room = keep - len(pinned)
    survivors_unpinned = unpinned[-room:] if room > 0 else []
    return pinned + survivors_unpinned


def tokenize(text: str) -> List[str]:
    """Simple tokenizer that splits on whitespace and removes punctuation."""
    return [word.strip('.,!?";') for word in text.split()]

def get_text_similarity(text1: str, text2: str) -> float:
    """Calculate Jaccard similarity between two texts."""
    if not text1 or not text2:
        return 0.0
    
    tokens1 = set(tokenize(text1.lower()))
    tokens2 = set(tokenize(text2.lower()))
    
    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0
        
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    
    return len(intersection) / len(union)

class MemoryManager:
    def __init__(self, data_dir: str):
        self.memory_file = os.path.join(data_dir, "memory.json")
        # GRAY-BRN-08: guards every read-modify-save (load_all -> mutate ->
        # save) critical section on memory.json. RLock (not Lock) so a
        # guarded method (e.g. add_and_save) can safely call another guarded
        # method (e.g. mutate) on the same thread without deadlocking.
        # Mirrors src/api_key_manager.py:18-21 / core/auth.py:98-106. This is
        # a PROCESS-LOCAL lock only -- it does NOT serialize against the
        # separate mcp_servers/memory_server.py subprocess, which opens its
        # own MemoryManager on the same memory.json file (30-RESEARCH.md
        # Pitfall 4 / Open Question 1).
        self._lock = threading.RLock()
        self.ensure_file_exists()

    def mutate(self, fn: Callable[[List[Dict]], Any]) -> Any:
        """Generic guarded read-modify-write primitive (GRAY-BRN-08).

        Runs `fn(entries)` under `self._lock` against the current entries
        loaded from disk, then persists whatever `fn` left in the list via
        `save()`, and returns `fn`'s return value. The lock spans the FULL
        load_all -> fn -> save span (not just save()) -- the race this
        closes is between one caller's load_all() and another caller's
        save(), not inside save() itself (30-RESEARCH.md Pitfall 1).
        """
        with self._lock:
            entries = self.load_all()
            result = fn(entries)
            self.save(entries)
            return result

    def add_and_save(
        self,
        text: str = None,
        *,
        entry: Optional[Dict] = None,
        source: str = "user",
        category: str = "fact",
        owner: str = None,
        session_id: str = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Guarded add: builds (or accepts) one entry and persists it under
        the lock, so a second concurrent add_and_save always sees the
        first's write (no lost update, GRAY-BRN-08)."""
        with self._lock:
            if entry is None:
                entry = self.add_entry(text, source=source, category=category, owner=owner)
            if session_id:
                entry["session_id"] = session_id
            if metadata:
                entry["metadata"] = dict(metadata)
            entries = self.load_all()
            entries.append(entry)
            self.save(entries)
            return entry

    def delete_by_id(self, memory_id: str, *, owner: str = None) -> Optional[Dict]:
        """Guarded delete-by-id. Honors an optional `owner` filter (only used
        by NativeMemoryProvider.forget) -- returns the removed entry, or None
        if no matching entry was found."""
        with self._lock:
            entries = self.load_all()
            target = None
            remaining = []
            for e in entries:
                if target is None and e.get("id") == memory_id and (owner is None or e.get("owner") == owner):
                    target = e
                    continue
                remaining.append(e)
            if target is None:
                return None
            self.save(remaining)
            return target

    def update_by_id(self, memory_id: str, *, owner: str = None, **fields) -> Optional[Dict]:
        """Guarded update-by-id: applies `fields` to the matching entry and
        persists it under the lock. Returns the updated entry, or None if no
        matching entry was found (or `owner` doesn't match)."""
        with self._lock:
            entries = self.load_all()
            for e in entries:
                if e.get("id") == memory_id and (owner is None or e.get("owner") == owner):
                    e.update(fields)
                    self.save(entries)
                    return e
            return None

    def compact(self, keep: int = MAX_MEMORY_ENTRIES) -> List[str]:
        """GRAY-BRN-06: opt-in, best-effort bounded eviction of memory.json.

        No-ops (returns []) unless the `brain_retention_enabled` setting is
        truthy -- mirrors src/brain_learn.py::is_enabled's
        `get_setting(key, default)` idiom, defaulting False so no existing
        store is pruned on upgrade before an operator explicitly opts in
        (30-RESEARCH.md Open Question 2).

        When enabled, prunes under `self._lock` (spanning the full
        load_all -> prune -> save span, GRAY-BRN-08) and returns the ids of
        entries evicted, so the caller (NativeMemoryProvider.remember) can
        remove the matching Chroma vectors and keep the vector store in
        sync with memory.json (the authoritative store).

        Never raises -- a prune failure must never break a write (mirrors
        src/tool_output_store.py::_prune_store's exception-swallowing,
        best-effort shape).
        """
        try:
            from src.settings import get_setting
            if not get_setting("brain_retention_enabled", False):
                return []
        except Exception as e:
            logger.debug("memory compact: could not read brain_retention_enabled: %s", e, exc_info=True)
            return []

        try:
            with self._lock:
                entries = self.load_all()
                before_ids = {e.get("id") for e in entries}
                pruned = prune_memory(entries, keep=keep)
                after_ids = {e.get("id") for e in pruned}
                evicted_ids = [i for i in before_ids - after_ids if i is not None]
                if evicted_ids:
                    self.save(pruned)
                return evicted_ids
        except Exception as e:
            logger.warning("memory compact: prune skipped (best-effort): %s", e, exc_info=True)
            return []

    def extract_memory_from_chat(self, chat_history: List[Dict], session_id: str = None) -> List[Dict]:
        """
        Extract memory entries from chat history as a fallback when LLM fails.
        
        Args:
            chat_history: List of chat messages with 'role' and 'content' keys
            session_id: Optional session ID to associate with extracted memories
            
        Returns:
            List of memory entries with text, timestamp, and optional session_id
        """
        memories = []
        
        for msg in chat_history:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                content = str(msg.get("content", ""))
                lines = content.split('\n')
                
                for line in lines:
                    line = line.strip()
                    # Look for bullet points or numbered lists that might contain memories
                    if re.match(r'^[-*•]|\d+\.', line):
                        # Extract the text after the bullet/number. Group both
                        # markers so the capture applies to either — the previous
                        # `^[-*•]|\d+\.\s*(.*)` put the group on the numbered branch
                        # only, so a bullet line matched with group(1)=None and
                        # crashed on .strip().
                        text_match = re.match(r'^(?:[-*•]|\d+\.)\s*(.*)', line)
                        if text_match:
                            text = text_match.group(1).strip()
                            if text:
                                memories.append({
                                    "text": text,
                                    "timestamp": int(datetime.now().timestamp()),
                                    "session_id": session_id
                                })
                    # If we see a heading that suggests memories
                    elif re.search(r'memory|fact|note|remember', line, re.I):
                        pass
                    # If we see a clear separator or end
                    elif re.match(r'^={3,}|-{3,}|_{3,}', line):
                        pass
                        
        return memories
        
    def process_inline_memory_command(self, message: str) -> Tuple[bool, str]:
        """
        Check if a message is an inline memory command (e.g. "remember: X").
        
        Args:
            message: The user message to check
            
        Returns:
            Tuple of (is_command, extracted_text) where is_command is True if 
            the message matches the memory command pattern
        """
        # Pattern for memory commands: "remember: X", "memorize: X", "save: X", etc.
        pattern = r'^(?:remember|memorize|save|note|store)[:\-]?\s+(.+)$'
        match = re.match(pattern, message.strip(), re.IGNORECASE)
        
        if match:
            return True, match.group(1).strip()
        else:
            return False, ""
    
    def ensure_file_exists(self):
        """Create memory file if it doesn't exist."""
        if not os.path.exists(self.memory_file):
            with open(self.memory_file, 'w', encoding='utf-8') as f:
                json.dump([], f, ensure_ascii=False, indent=2)
    
    def load_all(self) -> List[Dict]:
        """Load all memory entries from JSON file (unfiltered)."""
        if not os.path.exists(self.memory_file):
            return []

        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return self._validate_entries(data)
        except (json.JSONDecodeError, PermissionError) as e:
            logger.error("Error loading memory.json: %s", e)
            return self._migrate_from_legacy()

        return []

    def load(self, owner: str = None) -> List[Dict]:
        """Load memory entries, optionally filtered by owner."""
        entries = self.load_all()
        if owner is None:
            return entries
        return [e for e in entries if e.get("owner") == owner]

    def claim_ownerless(self, owner: str):
        """Assign all ownerless memory entries to the given owner."""
        entries = self.load_all()
        changed = False
        claimed = 0
        for entry in entries:
            if not entry.get("owner"):
                entry["owner"] = owner
                changed = True
                claimed += 1
        if changed:
            self.save(entries)
            logger.info("Claimed %d ownerless memories for %s", claimed, owner)
    
    def _validate_entries(self, entries: List[Dict]) -> List[Dict]:
        """Ensure all entries have required fields."""
        validated = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "id" not in entry:
                entry["id"] = str(uuid.uuid4())
            if "timestamp" not in entry:
                entry["timestamp"] = int(time.time())
            if "source" not in entry:
                entry["source"] = "unknown"
            if "category" not in entry:
                entry["category"] = "fact"
            if "uses" not in entry:
                entry["uses"] = 0
            validated.append(entry)
        return validated
    
    def _migrate_from_legacy(self) -> List[Dict]:
        """Migrate from old text format to JSON if needed."""
        legacy_path = os.path.join(os.path.dirname(self.memory_file), "memory.txt")
        if not os.path.exists(legacy_path):
            return []
            
        logger.info("Converting legacy memory.txt to new JSON format")
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            
            entries = []
            for line in lines:
                entries.append({
                    "id": str(uuid.uuid4()),
                    "text": line,
                    "timestamp": int(time.time()),
                    "source": "user",
                    "category": "fact"
                })
            
            self.save(entries)
            return entries
        except Exception as e:
            logger.error("Failed to convert legacy memory: %s", e)
            return []
    
    def save(self, entries: List[Dict]):
        """Save memory entries to JSON file."""
        # Validate entries before saving
        for entry in entries:
            if "id" not in entry:
                entry["id"] = str(uuid.uuid4())
            if "timestamp" not in entry:
                entry["timestamp"] = int(time.time())
            if "source" not in entry:
                entry["source"] = "user"
            if "category" not in entry:
                entry["category"] = "fact"
        
        # Use atomic write
        tmp_file = self.memory_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, self.memory_file)
    
    def add_entry(self, text: str, source: str = "user", category: str = "fact", owner: str = None) -> Dict:
        """Add a new memory entry."""
        if not text.strip():
            raise ValueError("Memory text cannot be empty")

        entry = {
            "id": str(uuid.uuid4()),
            "text": text.strip(),
            "timestamp": int(time.time()),
            "source": source,
            "category": category,
            "uses": 0,
        }
        if owner:
            entry["owner"] = owner
        return entry

    def increment_uses(self, ids: List[str]) -> None:
        """Bump the uses counter for each memory id. Called after a memory has
        actually been injected into a chat's context (not just retrieved)."""
        if not ids:
            return
        id_set = set(ids)
        entries = self.load_all()
        changed = False
        for e in entries:
            if e.get("id") in id_set:
                e["uses"] = int(e.get("uses", 0) or 0) + 1
                changed = True
        if changed:
            self.save(entries)
    
    def find_duplicates(self, text: str, entries: List[Dict] = None) -> List[Dict]:
        """Find duplicate memory entries based on text content."""
        if entries is None:
            entries = self.load()
            
        text_lower = text.strip().lower()
        return [entry for entry in entries if entry["text"].lower() == text_lower]
            
    def categorize_memory_by_relevance(self, message: str, memories: list):
        """Categorize memories by type and relevance"""
        categories = {
            "contacts": [],
            "preferences": [],
            "facts": [],
            "tasks": []
        }
        
        msg_lower = message.lower()
        
        for mem in memories:
            text_lower = mem["text"].lower()
            
            # Contact info
            if any(word in text_lower for word in ["phone", "email", "address", "lives", "works"]):
                if any(word in msg_lower for word in ["contact", "phone", "address", "email"]):
                    categories["contacts"].append(mem)
            
            # Personal preferences
            elif any(word in text_lower for word in ["likes", "dislikes", "prefers", "favorite"]):
                if any(word in msg_lower for word in ["like", "prefer", "favorite", "want"]):
                    categories["preferences"].append(mem)
            
            # Tasks and todos
            elif any(word in text_lower for word in ["todo", "task", "remind", "meeting"]):
                if any(word in msg_lower for word in ["todo", "task", "schedule", "remind"]):
                    categories["tasks"].append(mem)
            
            # General facts - only if very relevant
            else:
                if get_text_similarity(message, mem["text"]) > 0.4:
                    categories["facts"].append(mem)
        
        return categories

    def get_relevant_memories(
        self,
        query: str,
        memories: list,
        threshold: float = LEXICAL_FALLBACK_SIMILARITY_THRESHOLD,
        max_items: int = 8,
    ):
        """Get memories that are relevant to the query based on text similarity and semantic keyword matching."""
        if not memories or not query.strip():
            return []
            
        # Define keyword categories for semantic matching
        identity_words = ["name", "who", "i", "am", "called", "identity", "myself", "me", "my"]
        contact_words = ["phone", "email", "address", "contact", "number", "where", "located", "reach"]
        preference_words = ["like", "prefer", "favorite", "want", "love", "hate", "dislike", "enjoy", "interested"]
        task_words = ["todo", "task", "remind", "meeting", "appointment", "schedule", "deadline"]
        fact_words = ["what", "when", "where", "how", "why", "explain", "describe", "information", "know"]
        
        query_lower = query.lower()
        
        # Determine query type based on keywords
        query_type = None
        if any(word in query_lower for word in identity_words):
            query_type = "identity"
        elif any(word in query_lower for word in contact_words):
            query_type = "contact"
        elif any(word in query_lower for word in preference_words):
            query_type = "preference"
        elif any(word in query_lower for word in task_words):
            query_type = "task"
        elif any(word in query_lower for word in fact_words):
            query_type = "fact"
        
        relevant = []
        identity_memories = []
        other_memories = []
        
        # Separate identity memories from others
        for memory in memories:
            memory_text = memory["text"].lower()
            # Check if this is an identity memory (contains name patterns or identity indicators)
            is_identity = any([
                re.search(r'\b[A-Z][a-z]+ [A-Z][a-z]+\b', memory["text"]),
                any(word in memory_text for word in ["name is", "i'm", "i am", "called", "my name", "named", "call me"])
            ])
            if is_identity:
                identity_memories.append(memory)
            else:
                other_memories.append(memory)
        
        # For identity queries, include identity memories regardless of similarity,
        # but GRAY-BRN-13 count-caps this block at IDENTITY_MEMORY_INJECTION_CAP
        # (independent of max_items) -- otherwise a broad identity-keyword match
        # would flood recall with every identity memory at a fixed 0.9 score.
        # Keep the most-recently-added ones (memories list is append-ordered).
        if query_type == "identity" and identity_memories:
            # Give them high scores to ensure they're included first
            for memory in identity_memories[-IDENTITY_MEMORY_INJECTION_CAP:]:
                relevant.append((0.9, memory))  # High score for identity memories in identity queries
        
        # Process other memories with similarity scoring
        for memory in other_memories:
            memory_text = memory["text"].lower()
            memory_tokens = set(tokenize(memory_text))
            query_tokens = set(tokenize(query_lower))
            
            # Calculate base Jaccard similarity
            if not query_tokens or not memory_tokens:
                continue
                
            base_similarity = len(query_tokens & memory_tokens) / len(query_tokens | memory_tokens)
            final_score = base_similarity
            
            # Apply boosts based on semantic matching
            if query_type == "contact":
                # Boost memories with contact information
                has_contact_info = any(word in memory_text for word in ["@gmail.com", "@", ".com", 
                                                                     "phone", "number", "address", 
                                                                     "http", "www", "tel:"])
                if has_contact_info:
                    final_score *= 1.4  # 40% boost for contact-related memories
            
            elif query_type == "preference":
                # Boost memories with preference indicators
                has_preference = any(word in memory_text for word in ["like", "love", "hate", "dislike", 
                                                                   "prefer", "favorite", "enjoy", "interested"])
                if has_preference:
                    final_score *= 1.3  # 30% boost for preference-related memories
            
            elif query_type == "task":
                # Boost memories with task indicators
                has_task = any(word in memory_text for word in ["todo", "task", "remind", "meeting", 
                                                              "appointment", "schedule", "deadline", "need to"])
                if has_task:
                    final_score *= 1.3  # 30% boost for task-related memories
            
            # Always consider exact phrase matches as highly relevant
            if query.lower() in memory["text"].lower():
                final_score = max(final_score, 0.8)  # Ensure high relevance for exact matches
            
            # Include memory if it meets threshold after boosts
            if final_score >= threshold:
                relevant.append((final_score, memory))
        
        # Sort by final score (descending) and return top matches
        relevant.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in relevant[:max_items]]
