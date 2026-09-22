RESEARCH AGENT - FINAL LOCAL VERSION

DATABASE
Host: localhost
Port: 1522
Service: FREEPDB1

LOGIN BEHAVIOUR
- Login is checked directly against Oracle by Python.
- Username is converted to uppercase.
- Password is also converted to uppercase.
- With your current Oracle password NTU:
  RESEARCH / research / Research -> RESEARCH
  NTU / ntu / Ntu -> NTU
- Password is NOT sent to the AI.
- Password is NOT stored in the browser session cookie.
- Credentials stay only in the running Python process memory for the active session.

ARCHITECTURE
User question
-> AI generates SELECT SQL only
-> Python validates SQL
-> Python connects to Oracle
-> Oracle executes
-> Python returns result
-> User

IMPORTANT
- AI never connects directly to Oracle.
- AI never receives database credentials.
- Generated SQL uses old Oracle comma + WHERE relation style.
- JOIN keyword is not used.
- Only SELECT queries are accepted by the Python validator.
- Existing office Oracle 11g / Forms 6i / Reports 6i / Toad configuration is not changed.

SETUP
1. Extract this ZIP into a new folder.
2. Create venv if needed:
   python -m venv venv
3. Activate:
   .\venv\Scripts\Activate.ps1
4. Install:
   pip install -r requirements.txt
5. Copy:
   Copy-Item .env.example .env
6. Open .env and add your Groq API key.
7. Run:
   python app.py
8. Open:
   http://127.0.0.1:5000

ORACLE LOGIN
Use your Oracle username/password on the login page.
Current research example:
Username: RESEARCH
Password: your Oracle password

NOTE ABOUT PASSWORD CASE
The app intentionally converts the entered password to uppercase so your
current uppercase Oracle password can be entered in any letter case.
If you later change the Oracle password to a mixed-case password, remove
.upper() from the password line in login().

UI
- Completely redesigned professional login page.
- Completely redesigned research dashboard/chat.
- Responsive desktop/mobile layout.
- No old JK Spinning branding.
- No old dark teal template.


UI FINAL UPDATE
- FREEPDB1 is no longer displayed anywhere in the user interface.
- Oracle Database branding is visible in login and chat screens.
- NTU research branding/logo-style mark is visible in login and dashboard.
- Complete interface refreshed with a cleaner university/research design.
- Salary values and salary aggregates are rounded and shown without .0 / float style.
  Example: 66000.0 -> 66,000


LATEST UPDATE
- Sign out now fully clears the app login token, in-memory credentials,
  chat history and Flask session.
- Every Oracle query connection is already closed immediately after the query.
- EMPLOYEES metadata now includes:
  PHONE_NUMBER, EMAIL, JOINING_DATE, GENDER, CITY, ADDRESS.
- AI prompt updated so questions about these new fields are treated as SQL/database questions.
- Oracle branding now says: Oracle AI Database 26ai Free.
- NTU crest is included as a transparent local SVG asset on login and dashboard.


FINAL V2 UPDATE
- Initial greeting shortened to:
  "Hello! Ask me anything about the available employee data."
- The greeting no longer says "Employee Research Assistant".
- The greeting no longer lists database columns.
- If a requested employee field is not present in the schema, the AI explains
  that the specific information is not available instead of listing columns.
- "Sign out" label changed to "Disconnect Session".
- Added 1-minute inactivity timeout on the logged-in screen.
- Mouse movement, mouse click, keyboard activity, touch and scrolling reset
  the inactivity timer.
- After 60 seconds of no activity, the app calls the logout route and returns
  to the login page, clearing the active database login session.
