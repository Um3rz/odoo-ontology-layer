"""Registry of playbooks (A4.5). Triage chooses among these names. Phases 5-7 add the rest here."""
from app.agents.inventory.playbooks.user_request import PLAYBOOK as USER_REQUEST

PLAYBOOKS = {p.name: p for p in (USER_REQUEST,)}
