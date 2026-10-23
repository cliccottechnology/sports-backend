"""Canonical RBAC catalog: every module + action in the ERP.

Permission codes are `<module>.<action>`. Legacy coarse codes (`students.manage`
etc.) still work: HasPerm treats `<module>.manage` as granting every action of
that module, and the data migration maps legacy grants onto granular rows.
"""
from typing import Dict, List, Tuple

# module -> [(action, human label)]
MODULE_ACTIONS: Dict[str, List[Tuple[str, str]]] = {
    # `schools.manage` = platform-level academy onboarding. The granular
    # create/edit/archive codes cover a tenant academy's OWN branches and
    # locations, which a branch administrator must be able to run without
    # being able to onboard or alter another academy.
    "schools": [("view", "View schools and branches"), ("create", "Create branches"),
                ("edit", "Edit branches"), ("archive", "Archive branches"),
                ("manage", "Onboard and manage schools")],
    "sports": [("view", "View sports"), ("create", "Create sports"), ("edit", "Edit sports"),
               ("archive", "Archive sports"), ("export", "Export sports")],
    "students": [("view", "View students"), ("create", "Create students"), ("edit", "Edit students"),
                 ("archive", "Archive students"), ("export", "Export student records")],
    "parents": [("view", "View parents"), ("create", "Create parents"), ("edit", "Edit parents"),
                ("archive", "Archive parents"), ("export", "Export parent records")],
    "coaches": [("view", "View coaches"), ("create", "Create coaches"), ("edit", "Edit coaches"),
                ("archive", "Archive coaches"), ("export", "Export coach records")],
    "batches": [("view", "View batches"), ("create", "Create batches"), ("edit", "Edit batches"),
                ("archive", "Archive batches"), ("export", "Export batch records")],
    "sessions": [("view", "View sessions"), ("create", "Create sessions"), ("edit", "Edit sessions"),
                 ("archive", "Archive sessions"), ("export", "Export session records")],
    "attendance": [("view", "View attendance"), ("mark", "Mark attendance"),
                   ("correct", "Correct attendance"), ("export", "Export attendance reports")],
    "fees": [("view", "View fees"), ("create", "Create fee records"), ("edit", "Edit eligible fee records"),
             ("adjust", "Approve fee adjustments"), ("export", "Export fee reports")],
    "payments": [("view", "View payments"), ("create", "Initiate payments"),
                 ("verify", "Verify payments"), ("refund", "Approve refunds"), ("export", "Export payment records")],
    "trainer_fees": [("view", "View trainer fees"), ("manage", "Configure fee rules"),
                     ("generate", "Generate payables"), ("verify", "Verify payables"),
                     ("pay", "Approve trainer settlements"), ("adjust", "Approve trainer adjustments")],
    "whatsapp": [("view", "View WhatsApp log"), ("send", "Send WhatsApp messages"),
                 ("manage", "Manage WhatsApp templates")],
    "reports": [("view", "View reports"), ("export", "Export reports")],
    "notifications": [("view", "View notifications"), ("send", "Send notifications")],
    "documents": [("view", "View documents"), ("create", "Upload documents"), ("edit", "Edit documents"),
                  ("archive", "Archive documents"), ("export", "Export document records")],
    "support": [("view", "View support tickets"), ("create", "Raise support tickets"),
                ("reply", "Reply to tickets"), ("manage", "Manage all tickets (assign, status)")],
    "customization": [("view", "View customization requests"), ("create", "Submit customization requests"),
                      ("reply", "Comment / reply on requests"), ("review", "Review, approve, reject, clarify, prioritize"),
                      ("manage", "Assign work, track progress, reopen requests")],
    "users": [("view", "View users"), ("create", "Create users"), ("edit", "Edit users"),
              ("activate", "Activate / deactivate accounts"), ("reset_password", "Reset passwords")],
    "roles": [("view", "View roles"), ("manage", "Manage roles and permissions")],
    "settings": [("view", "View settings"), ("edit", "Edit settings")],
    "audit": [("view", "View audit logs")],
}

MODULE_ORDER = ["schools", "sports", "students", "parents", "coaches", "batches", "sessions",
                "attendance", "fees", "payments", "trainer_fees", "whatsapp", "reports",
                "notifications", "documents", "support", "customization", "users", "roles",
                "settings", "audit"]

ALL_CODES: List[Tuple[str, str, str]] = [
    (f"{mod}.{act}", label, mod) for mod in MODULE_ORDER for act, label in MODULE_ACTIONS[mod]
]

# Legacy coarse code -> granular codes it maps onto (for existing role grants).
LEGACY_MAP: Dict[str, List[str]] = {}
for _mod in ("sports", "students", "parents", "coaches", "batches", "sessions",
             "fees", "users", "trainer_fees"):
    LEGACY_MAP[f"{_mod}.manage"] = [f"{_mod}.{a}" for a, _ in MODULE_ACTIONS[_mod]]
LEGACY_MAP["masters.view"] = ["sports.view"]
LEGACY_MAP["masters.manage"] = [f"sports.{a}" for a, _ in MODULE_ACTIONS["sports"]]
LEGACY_MAP["attendance.mark"] = ["attendance.mark", "attendance.view"]
LEGACY_MAP["attendance.correct"] = ["attendance.correct", "attendance.view"]
LEGACY_MAP["payments.verify"] = ["payments.verify", "payments.view"]
LEGACY_MAP["settings.manage"] = ["settings.view", "settings.edit"]

# View-only starter set for school/academy admins created by the Super Admin.
SCHOOL_ADMIN_VIEWS = [
    "sports.view", "students.view", "parents.view", "coaches.view", "batches.view",
    "sessions.view", "attendance.view", "fees.view", "payments.view", "trainer_fees.view",
    "whatsapp.view", "reports.view", "notifications.view", "documents.view",
]
