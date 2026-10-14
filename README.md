# Employee Attendance & Analytics API

A REST API built with **FastAPI, Python, and MongoDB** for employee management, attendance tracking, regularization, and workforce analytics.

## Features

- Employee management with pagination and filtering
- Attendance punch-in and punch-out
- Attendance regularization with audit history
- Monthly attendance reports and department summaries
- Late-attendance leaderboard
- Department attendance trends with seven-day moving averages
- MongoDB query execution analysis

## Tech Stack

Python · FastAPI · Pydantic · PyMongo · MongoDB

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   pip install -r requirements.txt
   

3. Configure `MONGO_URI` and `MONGO_DB` in your local `.env` file.
4. Ensure MongoDB is running and load the sample data:
   python sample_seed.py
  

5. Start the application:
   python -m uvicorn app.main:app --port 8000
  

## API Documentation

Once the application is running, visit:

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Project Structure

- `app/main.py` — API implementation
- `sample_data/` — Sample employee and attendance data
- `sample_seed.py` — Database seeding script
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API contract
- `DATA_MODEL.md` — Database model
- `REVIEW.md` — Starter-code review
- `DECISIONS.md` — Design decisions

## Notes

Requires MongoDB 6.0 or newer. Do not commit `.env`, `.venv`, or secrets to the repository.