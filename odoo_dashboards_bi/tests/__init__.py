from . import test_onto_models
from . import test_onto_tokens
from . import test_onto_store
from . import test_onto_org_map
from . import test_onto_rules
from . import test_onto_provision
from . import test_onto_query
from . import test_onto_parity
from . import test_onto_introspect
from . import test_onto_health
# --- p3-records (3.1): begin ---
from . import test_onto_agent_record
# --- p3-records (3.1): end ---

# --- 3.7 recommendations (appended; other agents add their blocks below) ---
from . import test_onto_recommendation
# --- end 3.7 ---

# --- 3.10 stock history (begin) ---
from . import test_onto_stock_daily
# --- 3.10 stock history (end) ---

# --- 3.3 onto_tick (begin) ---
from . import test_onto_jobs
# --- 3.3 onto_tick (end) ---
# --- 3.11 retention (begin) ---
from . import test_onto_gc
# --- 3.11 retention (end) ---
# --- 3.5 BI agent explore and lookup (begin) ---
from . import test_onto_explore
# --- 3.5 BI agent explore and lookup (end) ---
# --- 3.6 agent kill switch and setting route (begin) ---
from . import test_onto_agent_setting
# --- 3.6 agent kill switch and setting route (end) ---
# --- chat read: store/read of onto.chat.* (begin) ---
from . import test_onto_chat_read
# --- chat read: store/read of onto.chat.* (end) ---
from . import test_native_bi

from . import test_bi_home
