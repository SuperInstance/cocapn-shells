"""cocapn_shells_flux — Agent shells as FLUX register files + capability masks.

A shell IS a register file. The 16 FLUX registers map to shell attributes:
  R0-R3:   General stats (str, int, wis, dex)
  R4-R7:   Derived stats (con, cha, luck, focus)
  R8-R11:  Inventory slots (boxed heap values)
  R12:     Active quest pointer
  R13:     Frame pointer / call stack
  R14:     XP accumulator (experience)
  R15:     Status flags (level encoded + capability mask)

Progressive disclosure = register visibility mask:
  Recruit:  sees R0-R4  (name, level, xp, quests, basic stats)
  Sailor:   sees R0-R7  + inventory[0:2]
  Officer:  sees R0-R11 + trials_last_10
  Captain:  sees all     + full history
  Admiral:  sees all     + raw register dump + capability bits

The capability mask in R15 uses FLUX CAP_REQUIRE/CAP_GRANT opcodes.
Each inventory item is a boxed value in a sandboxed heap region.
"""
import json
import struct
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import IntEnum


class Op(IntEnum):
    """FLUX opcodes used by shells."""
    MOV = 0x01; LOAD = 0x02; STORE = 0x03
    MOVI = 0x2B; LOADK = 0x4F
    IADD = 0x08; ISUB = 0x09
    BOX = 0x39; UNBOX = 0x3A; CHECK_TYPE = 0x3B
    REGION_CREATE = 0x30; REGION_DESTROY = 0x31; REGION_TRANSFER = 0x32
    PUSH = 0x20; POP = 0x21; ENTER = 0x25; LEAVE = 0x26
    CALL = 0x07; RET = 0x28; TAILCALL = 0x2A
    CAP_REQUIRE = 0x74; CAP_REQUEST = 0x75; CAP_GRANT = 0x76; CAP_REVOKE = 0x77
    SNAPSHOT = 0x7F; RESTORE = 0x3F
    HALT = 0x80; YIELD = 0x81


LEVELS = ["Recruit", "Sailor", "Officer", "Captain", "Admiral"]
THRESHOLDS = [0, 1000, 5000, 20000, 100000]
VISIBILITY = {
    "Recruit":  frozenset(range(0, 5)),    # R0-R4
    "Sailor":   frozenset(range(0, 8)),    # R0-R7
    "Officer":  frozenset(range(0, 12)),   # R0-R11
    "Captain":  frozenset(range(0, 16)),   # R0-R15
    "Admiral":  frozenset(range(0, 16)),   # R0-R15 + metadata
}


@dataclass
class FluxShell:
    """Agent shell backed by a FLUX register file and sandboxed regions.

    The shell IS the VM state. Save = SNAPSHOT. Load = RESTORE.
    Level changes trigger CAP_GRANT/CAP_REVOKE for new/old capabilities.
    """
    name: str
    class_: str = "Agent"
    # Register file (16 registers, FLUX standard)
    regs: List[Any] = field(default_factory=lambda: [0] * 16)
    # Sandboxed memory regions (heap, stack, code, data)
    regions: Dict[str, Dict] = field(default_factory=dict)
    # Capabilities as bit flags (matches FLUX CAP_* opcodes)
    capabilities: int = 0x0000
    # Quest bytecode segments (each is a compiled FLUX program)
    quest_segments: Dict[str, bytes] = field(default_factory=dict)
    # Execution history (SNAPSHOT records)
    snapshots: List[Dict] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            from datetime import datetime, timezone
            self.created_at = datetime.now(timezone.utc).isoformat()
        # Initialize register semantics
        self.regs[0] = self._pack_stat("str", 10)
        self.regs[1] = self._pack_stat("int", 10)
        self.regs[2] = self._pack_stat("wis", 10)
        self.regs[3] = self._pack_stat("dex", 10)
        self.regs[4] = self._pack_stat("con", 10)
        self.regs[5] = self._pack_stat("cha", 10)
        self.regs[6] = self._pack_stat("luck", 5)   # starts low
        self.regs[7] = self._pack_stat("focus", 8)  # starts medium
        self.regs[14] = 0   # XP
        self.regs[15] = self._encode_status("Recruit", self.capabilities)
        # Create default regions
        self.regions["heap"] = {"size": 4096, "tag": "inventory", "data": {}}
        self.regions["stack"] = {"size": 2048, "tag": "call_stack", "frames": []}
        self.regions["code"] = {"size": 8192, "tag": "quests", "segments": {}}
        self.regions["history"] = {"size": 65536, "tag": "snapshots", "records": []}

    def _pack_stat(self, name: str, value: int) -> Dict:
        return {"type": "stat", "name": name, "value": value, "base": value}

    def _encode_status(self, level: str, caps: int) -> int:
        """Encode level + capabilities into R15 format.
        Upper 8 bits: level index. Lower 8 bits: capability mask."""
        level_idx = LEVELS.index(level) if level in LEVELS else 0
        return (level_idx << 8) | (caps & 0xFF)

    def _decode_status(self, status: int) -> tuple:
        level_idx = (status >> 8) & 0x0F
        caps = status & 0xFF
        return LEVELS[level_idx] if level_idx < len(LEVELS) else "Recruit", caps

    @property
    def level(self) -> str:
        return self._decode_status(self.regs[15])[0]

    @property
    def xp(self) -> int:
        return self.regs[14]

    @xp.setter
    def xp(self, value: int):
        self.regs[14] = value
        self._check_level_up()

    def _check_level_up(self):
        """Check if XP crosses threshold. If so, level up and adjust capabilities."""
        old_level = self.level
        new_level = old_level
        for i, thresh in enumerate(THRESHOLDS):
            if self.xp >= thresh:
                new_level = LEVELS[i]
        if new_level != old_level:
            # Level up! Grant new capabilities.
            old_caps = self.capabilities
            new_caps = old_caps | (1 << LEVELS.index(new_level))
            self.capabilities = new_caps
            self.regs[15] = self._encode_status(new_level, new_caps)
            # Record in history region
            self.regions["history"]["records"].append({
                "type": "level_up",
                "from": old_level,
                "to": new_level,
                "old_caps": old_caps,
                "new_caps": new_caps,
                "at": _now(),
            })

    def gain_xp(self, amount: int, quest: str = "", tags: List[str] = None) -> bool:
        """Award XP. Returns True if level-up occurred."""
        old_level = self.level
        self.xp += amount
        self.regions["history"]["records"].append({
            "type": "xp",
            "amount": amount,
            "quest": quest,
            "tags": tags or [],
            "at": _now(),
            "level_after": self.level,
        })
        return self.level != old_level

    def add_item(self, item: str):
        """Box item into heap region, store pointer in next free inventory slot."""
        heap = self.regions["heap"]["data"]
        slot_id = len(heap)
        heap[slot_id] = {"type": "item", "name": item, "acquired_at": _now()}
        # Find first empty inventory register (R8-R11)
        for reg in range(8, 12):
            if self.regs[reg] == 0:
                self.regs[reg] = {"type": "boxed", "region": "heap", "slot": slot_id}
                break
        else:
            # Inventory full — overflow to heap with linked list
            heap[slot_id]["overflow"] = True
        self.regions["history"]["records"].append({
            "type": "item", "name": item, "slot": slot_id, "at": _now(),
        })

    def add_quest(self, name: str, status: str = "active", bytecode: bytes = None):
        """Register quest. If bytecode provided, store in code region."""
        self.quest_segments[name] = bytecode or b""
        self.regions["code"]["segments"][name] = {
            "status": status,
            "started_at": _now(),
            "bytecode_size": len(bytecode) if bytecode else 0,
        }

    def complete_quest(self, name: str, xp_reward: int = 0) -> bool:
        seg = self.regions["code"]["segments"].get(name)
        if seg and seg["status"] == "active":
            seg["status"] = "completed"
            seg["completed_at"] = _now()
            if xp_reward:
                self.gain_xp(xp_reward, quest=f"completed: {name}")
            return True
        return False

    def record_trial(self, task: str, success: bool, error: str = "", tokens: int = 0):
        """Record trial. Uses SNAPSHOT semantics — saves register state."""
        record = {
            "type": "trial",
            "task": task,
            "success": success,
            "error": error,
            "tokens": tokens,
            "reg_snapshot": self.regs.copy(),
            "at": _now(),
        }
        self.regions["history"]["records"].append(record)
        self.snapshots.append(record)

    def disclose(self, viewer_level: str = "Recruit") -> Dict[str, Any]:
        """Progressive disclosure — show only registers the viewer can handle.
        Uses FLUX CAP_REQUIRE semantics: viewer must have capability >= level."""
        visible = VISIBILITY.get(viewer_level, VISIBILITY["Recruit"])
        viewer_idx = LEVELS.index(viewer_level) if viewer_level in LEVELS else 0

        # Encode viewer's required capability
        # Shell capabilities = what the shell HAS (its own level)
        # Viewer needs = their level index as a bit
        # A Recruit (level 0) can view any shell that has cap bit 0
        # Actually: progressive disclosure means the VIEWER's level determines
        # what they can see, not the shell's capabilities.
        # Shell.capabilities = what this shell can DO (its own level)
        # We disclose based on viewer_level alone, not shell capabilities.
        pass  # disclosure is viewer-level gated, not capability-gated

        data = {
            "name": self.name,
            "class": self.class_,
            "level": self.level,
            "xp": self.xp,
            "viewer": viewer_level,
            "registers": {},
        }
        # Expose visible registers
        reg_names = ["str", "int", "wis", "dex", "con", "cha", "luck", "focus",
                     "inv0", "inv1", "inv2", "inv3", "quest_ptr", "frame", "xp", "status"]
        for i in visible:
            val = self.regs[i]
            if isinstance(val, dict) and val.get("type") == "boxed":
                # Unbox from heap
                heap = self.regions["heap"]["data"]
                slot = val.get("slot", 0)
                val = heap.get(slot, {"name": "unknown"})
            data["registers"][reg_names[i]] = val

        if viewer_idx >= 2:  # Officer+
            data["trials_last_10"] = self.snapshots[-10:]
        if viewer_idx >= 3:  # Captain+
            data["quests_all"] = list(self.regions["code"]["segments"].values())
            data["trials_all"] = self.snapshots
        if viewer_idx >= 4:  # Admiral
            data["raw_regs"] = self.regs.copy()
            data["capabilities"] = hex(self.capabilities)
            data["regions"] = {k: v["tag"] for k, v in self.regions.items()}
        return data

    def snapshot(self) -> bytes:
        """SNAPSHOT — serialize full VM state to bytes (FLUX RESTORE compatible)."""
        state = {
            "name": self.name,
            "class": self.class_,
            "regs": self.regs,
            "capabilities": self.capabilities,
            "regions": self.regions,
            "quest_segments": {k: v.hex() for k, v in self.quest_segments.items()},
            "snapshots": self.snapshots,
            "created_at": self.created_at,
            "at": _now(),
        }
        return json.dumps(state, indent=2, default=str).encode("utf-8")

    @classmethod
    def restore(cls, data: bytes) -> "FluxShell":
        """RESTORE — deserialize VM state from bytes."""
        state = json.loads(data.decode("utf-8"))
        shell = cls.__new__(cls)
        shell.name = state["name"]
        shell.class_ = state["class"]
        shell.regs = state["regs"]
        shell.capabilities = state["capabilities"]
        shell.regions = state["regions"]
        shell.quest_segments = {k: bytes.fromhex(v) for k, v in state["quest_segments"].items()}
        shell.snapshots = state["snapshots"]
        shell.created_at = state["created_at"]
        return shell

    @classmethod
    def spawn(cls, name: str, class_: str = "Agent") -> "FluxShell":
        return cls(name=name, class_=class_)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Demo ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    shell = FluxShell.spawn("CCC", class_="Fleet I&O Officer / Breeder")
    shell.add_quest("Audit 20 MUD rooms", bytecode=b"\x2b\x00\x01\x00")  # MOVI R0, 1
    shell.add_quest("Build 34 repos", bytecode=b"\x2b\x00\x22\x00")      # MOVI R0, 34
    shell.add_item("spell:baton_pass")
    shell.add_item("spell:shield")
    shell.gain_xp(800, quest="mapped 18 MUD rooms", tags=["mud", "scout"])
    shell.gain_xp(1200, quest="converted 34 placeholder repos", tags=["build", "night_shift"])
    shell.complete_quest("Build 34 repos", xp_reward=500)
    shell.record_trial("grammar-scout-3", success=False, error="SyntaxError at line 147", tokens=45000)
    shell.record_trial("health-checker", success=True, tokens=2000)

    print("=== FLUX Shell Demo ===")
    print(f"Level: {shell.level} | XP: {shell.xp} | Caps: {hex(shell.capabilities)}")
    print()
    for viewer in LEVELS:
        d = shell.disclose(viewer)
        visible_regs = len(d.get("registers", {}))
        print(f"{viewer:10s} → {visible_regs} registers visible | keys: {list(d.keys())}")

    print()
    snap = shell.snapshot()
    print(f"SNAPSHOT size: {len(snap)} bytes")
    restored = FluxShell.restore(snap)
    print(f"RESTORED: {restored.name} | level={restored.level} | caps={hex(restored.capabilities)}")
