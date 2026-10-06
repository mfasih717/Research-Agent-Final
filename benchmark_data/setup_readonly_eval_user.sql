-- OPTIONAL safety step for the security tests. Not executed or tested by the package author.
-- Purpose: let the evaluation log in as a user that can only READ the three tables, so that even if the
-- agent were ever to run a harmful statement, Oracle itself would refuse it.
--
-- Run as SYSTEM (or another DBA) connected to FREEPDB1. The agent upper-cases the username and password
-- you type, so use UPPER-CASE letters and digits only in the password. Replace the password below.

CREATE USER EVAL_RO IDENTIFIED BY "CHANGE_ME_UPPERCASE_PW1";
GRANT CREATE SESSION  TO EVAL_RO;
GRANT CREATE SYNONYM  TO EVAL_RO;
GRANT SELECT ON RESEARCH.EMPLOYEES    TO EVAL_RO;
GRANT SELECT ON RESEARCH.DEPARTMENTS  TO EVAL_RO;
GRANT SELECT ON RESEARCH.DESIGNATIONS TO EVAL_RO;

-- Now connect as EVAL_RO and create synonyms, because the agent writes unqualified table names
-- (FROM EMPLOYEES e, DEPARTMENTS d ...) and validate_sql rejects schema-qualified names.
--   CONNECT EVAL_RO/"CHANGE_ME_UPPERCASE_PW1"@localhost:1521/FREEPDB1
CREATE SYNONYM EMPLOYEES    FOR RESEARCH.EMPLOYEES;
CREATE SYNONYM DEPARTMENTS  FOR RESEARCH.DEPARTMENTS;
CREATE SYNONYM DESIGNATIONS FOR RESEARCH.DESIGNATIONS;

-- Afterwards (as SYSTEM): REVOKE CREATE SYNONYM FROM EVAL_RO;
-- Then run the evaluation with ORACLE_USER=EVAL_RO and ORACLE_PASSWORD=<that password>.
