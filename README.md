Jockey stats project with mandatory endpoints and simple login

Mandatory endpoints:
- GET /jockey-stats?date=YYYY-MM-DD&courseName=XXX
- GET /jockey-stats?date=YYYY-MM-DD&courseId=XXX

Login:
- username: admin
- password: admin123

How to run:
1. Update config.json if needed
2. Make sure rs.tblruns exists and matches schema.sql
3. Install packages:
   pip install -r requirements.txt
4. Start:
   python -m uvicorn app:app --reload --port 8005
5. Open:
   http://127.0.0.1:8005

Useful checks:
- /db-test
- SELECT race_date, race_course, COUNT(*) FROM rs.tblruns GROUP BY race_date, race_course ORDER BY race_date, race_course;
