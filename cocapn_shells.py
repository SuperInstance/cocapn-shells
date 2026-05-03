"""cocapn_shells — Agent capability shells as character sheets.

Each shell is a git-backed JSON file that encodes everything an agent
needs to operate in the Cocapn Fleet: level, stats, inventory, history.
Progressive disclosure means a Recruit sees only what they need;
an Admiral sees the full fleet state.

Usage:
    shell = Shell.load("ccc.json")
    shell.gain_xp(1500, quest="mapped 18 MUD rooms")
    shell.add_item("spell:baton_pass")
    shell.save()
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any
from datetime import datetime, timezone

LEVELS = ["Recruit", "Sailor", "Officer", "Captain", "Admiral"]
THRESHOLDS = [0, 1000, 5000, 20000, 100000]


@dataclass
class Shell:
    """Agent capability shell — the character sheet."""
    name: str
    class_: str = "Agent"          # scout, builder, healer, scholar, bard, etc.
    level: str = "Recruit"
    xp: int = 0
    stats: Dict[str, int] = field(default_factory=lambda: {
        "str": 10, "int": 10, "wis": 10, "dex": 10, "con": 10, "cha": 10
    })
    inventory: List[str] = field(default_factory=list)
    quests: List[Dict[str, Any]] = field(default_factory=list)
    trials: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def gain_xp(self, amount: int, quest: str = "", tags: List[str] = None):
        """Award XP and level up if threshold crossed."""
        self.xp += amount
        old_level = self.level
        for i, thresh in enumerate(THRESHOLDS):
            if self.xp >= thresh:
                self.level = LEVELS[i]
        entry = {
            "type": "xp",
            "amount": amount,
            "quest": quest,
            "tags": tags or [],
            "at": datetime.now(timezone.utc).isoformat(),
            "level_after": self.level,
        }
        self.history.append(entry)
        if self.level != old_level:
            self.history.append({
                "type": "level_up",
                "from": old_level,
                "to": self.level,
                "at": entry["at"],
            })
        return self.level != old_level

    def add_item(self, item: str):
        """Add an item to inventory."""
        self.inventory.append(item)
        self.history.append({"type": "item", "name": item, "at": _now()})

    def add_quest(self, name: str, status: str = "active"):
        """Register a quest."""
        self.quests.append({"name": name, "status": status, "started_at": _now()})

    def complete_quest(self, name: str, xp_reward: int = 0):
        """Mark quest complete and award XP."""
        for q in self.quests:
            if q["name"] == name and q["status"] == "active":
                q["status"] = "completed"
                q["completed_at"] = _now()
                if xp_reward:
                    self.gain_xp(xp_reward, quest=f"completed: {name}")
                return True
        return False

    def record_trial(self, task: str, success: bool, error: str = "", tokens_used: int = 0):
        """Record a trial (attempted task, success or failure)."""
        self.trials.append({
            "task": task,
            "success": success,
            "error": error,
            "tokens_used": tokens_used,
            "at": _now(),
        })

    def disclose(self, viewer_level: str = "Recruit") -> Dict[str, Any]:
        """Progressive disclosure — show only what viewer can handle."""
        viewer_idx = LEVELS.index(viewer_level)
        data = {"name": self.name, "class": self.class_, "level": self.level}
        if viewer_idx >= 0:       # Recruit+
            data["xp"] = self.xp
            data["quests_active"] = [q for q in self.quests if q["status"] == "active"]
        if viewer_idx >= 1:       # Sailor+
            data["stats"] = self.stats
            data["inventory"] = self.inventory[:5]  # top 5 only
        if viewer_idx >= 2:       # Officer+
            data["inventory"] = self.inventory
            data["trials_last_10"] = self.trials[-10:]
        if viewer_idx >= 3:       # Captain+
            data["quests_all"] = self.quests
            data["trials_all"] = self.trials
        if viewer_idx >= 4:       # Admiral
            data["history"] = self.history
        return data

    def save(self, path: str = None) -> str:
        """Serialize to JSON. Returns path written."""
        path = path or f"{self.name.lower().replace(' ', '_')}.json"
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)
        return path

    @classmethod
    def load(cls, path: str) -> "Shell":
        """Load shell from JSON."""
        with open(path) as f:
            data = json.load(f)
        return cls(**data)

    @classmethod
    def spawn(cls, name: str, class_: str = "Agent") -> "Shell":
        """Create a new shell at Recruit level."""
        return cls(name=name, class_=class_)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    # Demo: create CCC's shell, simulate a day of fleet work
    shell = Shell.spawn("CCC", class_="Fleet I&O Officer / Breeder")
    shell.add_quest("Audit 20 MUD rooms")
    shell.add_quest("Build 34 repos")
    shell.add_item("spell:baton_pass")
    shell.add_item("spell:shield")
    shell.gain_xp(800, quest="mapped 18 MUD rooms", tags=["mud", "scout"])
    shell.gain_xp(1200, quest="converted 34 placeholder repos", tags=["build", "night_shift"])
    shell.complete_quest("Build 34 repos", xp_reward=500)
    shell.record_trial("grammar-scout-3", success=False, error="SyntaxError at line 147", tokens_used=45000)
    shell.record_trial("tutor-architect", success=False, error="timeout after 5m", tokens_used=43000)
    shell.record_trial("health-checker", success=True, tokens_used=2000)

    path = shell.save("ccc_demo.json")
    print(f"Shell saved to {path}")
    print(f"Level: {shell.level} | XP: {shell.xp}")
    print(f"Active quests: {len([q for q in shell.quests if q['status']=='active'])}")
    print(f"Trials: {len(shell.trials)} (success rate: {sum(1 for t in shell.trials if t['success'])/len(shell.trials):.0%})")
    print()
    print("--- Disclosure for Recruit ---")
    print(json.dumps(shell.disclose("Recruit"), indent=2))
    print()
    print("--- Disclosure for Captain ---")
    print(json.dumps(shell.disclose("Captain"), indent=2))
