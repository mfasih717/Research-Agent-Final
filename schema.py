"""
Local Oracle Research Schema Metadata

Actual database:
Oracle AI Database 26ai Free
Service: FREEPDB1
Schema/User: RESEARCH
"""

# ---- Table 1: Employees ----
EMPLOYEES_TABLE = "EMPLOYEES"
EMPLOYEES_PRIMARY_KEY = "ECODE"

EMPLOYEES_COLUMNS = [
    "ECODE",
    "EMP_NAME",
    "DEPT_CODE",
    "DESG_CODE",
    "SALARY",
    "PHONE_NUMBER",
    "EMAIL",
    "JOINING_DATE",
    "GENDER",
    "CITY",
    "ADDRESS"
]

# ---- Table 2: Departments ----
DEPARTMENTS_TABLE = "DEPARTMENTS"
DEPARTMENTS_PRIMARY_KEY = "DEPT_CODE"

DEPARTMENTS_COLUMNS = [
    "DEPT_CODE",
    "DEPT_NAME"
]

# ---- Table 3: Designations ----
DESIGNATIONS_TABLE = "DESIGNATIONS"
DESIGNATIONS_PRIMARY_KEY = "DESG_CODE"

DESIGNATIONS_COLUMNS = [
    "DESG_CODE",
    "DESG_NAME"
]

ALLOWED_TABLES = {
    EMPLOYEES_TABLE,
    DEPARTMENTS_TABLE,
    DESIGNATIONS_TABLE
}

EMPLOYEE_RELATIONS = {
    "DEPT_CODE": {
        "table": DEPARTMENTS_TABLE,
        "target_pk": "DEPT_CODE",
        "name_column": "DEPT_NAME",
        "label": "DEPARTMENT"
    },
    "DESG_CODE": {
        "table": DESIGNATIONS_TABLE,
        "target_pk": "DESG_CODE",
        "name_column": "DESG_NAME",
        "label": "DESIGNATION"
    }
}


def build_schema_description():
    lines = []

    lines.append(
        f"TABLE {EMPLOYEES_TABLE} "
        f"(primary key: {EMPLOYEES_PRIMARY_KEY}):"
    )
    lines.append("  Columns: " + ", ".join(EMPLOYEES_COLUMNS))

    lines.append(
        f"TABLE {DEPARTMENTS_TABLE} "
        f"(primary key: {DEPARTMENTS_PRIMARY_KEY}):"
    )
    lines.append("  Columns: " + ", ".join(DEPARTMENTS_COLUMNS))

    lines.append(
        f"TABLE {DESIGNATIONS_TABLE} "
        f"(primary key: {DESIGNATIONS_PRIMARY_KEY}):"
    )
    lines.append("  Columns: " + ", ".join(DESIGNATIONS_COLUMNS))

    return "\n".join(lines)


def build_relations_description():
    lines = []

    for column, relation in EMPLOYEE_RELATIONS.items():
        lines.append(
            f"- EMPLOYEES.{column} -> "
            f"{relation['table']}.{relation['target_pk']} "
            f"(use {relation['name_column']} for the readable name)"
        )

    return "\n".join(lines)
