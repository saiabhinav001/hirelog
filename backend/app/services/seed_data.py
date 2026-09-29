from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

from firebase_admin import firestore

from app.core.firebase import db
from app.services.faiss_store import faiss_store
from app.services.nlp import pipeline


SEED_VERSION = "v1"
SEED_COUNT = 180
SEED_UID = "seed-bot"


FIRST_NAMES = [
    "Aarav",
    "Vivaan",
    "Aditya",
    "Vihaan",
    "Arjun",
    "Ishaan",
    "Rohan",
    "Karthik",
    "Rahul",
    "Aman",
    "Nikhil",
    "Kunal",
    "Varun",
    "Pranav",
    "Siddharth",
    "Abhishek",
    "Akash",
    "Saurabh",
    "Yash",
    "Manish",
    "Gaurav",
    "Rohit",
    "Deepak",
    "Harsha",
    "Vivek",
    "Ajay",
    "Arvind",
    "Aditi",
    "Ananya",
    "Riya",
    "Priya",
    "Sneha",
    "Pooja",
    "Shreya",
    "Tanvi",
    "Sanya",
    "Kavya",
    "Meera",
    "Isha",
    "Neha",
    "Kriti",
    "Aishwarya",
    "Nandini",
    "Divya",
    "Simran",
    "Mansi",
]

LAST_NAMES = [
    "Sharma",
    "Verma",
    "Gupta",
    "Mehta",
    "Singh",
    "Patel",
    "Jain",
    "Iyer",
    "Nair",
    "Reddy",
    "Rao",
    "Das",
    "Banerjee",
    "Chatterjee",
    "Bhattacharya",
    "Kulkarni",
    "Deshmukh",
    "Joshi",
    "Kapoor",
    "Malhotra",
    "Bansal",
    "Sethi",
    "Kaur",
    "Gill",
    "Yadav",
    "Mishra",
    "Tiwari",
    "Agarwal",
    "Saxena",
    "Ghosh",
    "Mukherjee",
    "Menon",
    "Pillai",
    "Varma",
    "Prasad",
    "Shetty",
    "Nayak",
    "Arora",
    "Tripathi",
    "Pandey",
    "Srivastava",
    "Malik",
    "Thakur",
    "Bhat",
    "Sinha",
]

COLLEGES = [
    "IIT Delhi",
    "IIT Bombay",
    "IIT Madras",
    "IIT Kanpur",
    "IIT Kharagpur",
    "NIT Trichy",
    "NIT Warangal",
    "NIT Surathkal",
    "BITS Pilani",
    "BITS Goa",
    "BITS Hyderabad",
    "VIT Vellore",
    "SRM KTR",
    "Manipal Institute of Technology",
    "IIIT Hyderabad",
    "IIIT Bangalore",
    "IIIT Allahabad",
    "DTU",
    "NSUT",
    "PES University",
    "RV College of Engineering",
    "COEP Pune",
    "Jadavpur University",
    "Anna University",
    "Thapar Institute of Engineering",
    "Amrita Vishwa Vidyapeetham",
    "PSG Tech",
]

COMPANIES = [
    ("TCS", ["Assistant System Engineer", "Digital Engineer"]),
    ("Infosys", ["System Engineer", "Power Programmer"]),
    ("Wipro", ["Project Engineer", "Software Engineer"]),
    ("HCL", ["Software Engineer", "Graduate Engineer Trainee"]),
    ("Tech Mahindra", ["Associate Software Engineer", "Analyst"]),
    ("Cognizant", ["Programmer Analyst", "Associate"]),
    ("Accenture", ["Associate Software Engineer", "Business Analyst"]),
    ("Capgemini", ["Software Engineer", "Senior Analyst"]),
    ("IBM India", ["Software Engineer", "Data Engineer"]),
    ("Deloitte USI", ["Analyst", "Consultant"]),
    ("EY GDS", ["Technology Analyst", "Advisory Analyst"]),
    ("KPMG India", ["Analyst", "Associate Consultant"]),
    ("PwC India", ["Associate", "Technology Consultant"]),
    ("Oracle India", ["Software Engineer", "Cloud Engineer"]),
    ("SAP Labs India", ["Developer Associate", "QA Engineer"]),
    ("Adobe India", ["Software Engineer", "SDE Intern"]),
    ("Microsoft India", ["Software Engineer", "Support Engineer"]),
    ("Google India", ["Software Engineer", "SWE Intern"]),
    ("Amazon India", ["SDE", "SDE Intern"]),
    ("Flipkart", ["Software Engineer", "Backend Engineer"]),
    ("Paytm", ["Software Engineer", "Backend Engineer"]),
    ("PhonePe", ["Software Engineer", "Risk Analyst"]),
    ("Razorpay", ["Software Engineer", "SDE Intern"]),
    ("Swiggy", ["Software Engineer", "Data Analyst"]),
    ("Zomato", ["Software Engineer", "Product Analyst"]),
    ("Ola", ["Software Engineer", "Data Analyst"]),
    ("Uber India", ["Software Engineer", "Support Engineer"]),
    ("Walmart Global Tech India", ["Software Engineer", "Data Engineer"]),
    ("Goldman Sachs India", ["Analyst", "Associate"]),
    ("J.P. Morgan India", ["Software Engineer", "Analyst"]),
    ("Barclays India", ["Developer", "Analyst"]),
    ("Morgan Stanley India", ["Technology Analyst", "Associate"]),
    ("DE Shaw India", ["Software Engineer", "Analyst"]),
    ("Nagarro", ["Software Engineer", "Full Stack Developer"]),
    ("Publicis Sapient", ["Associate Technology", "Software Engineer"]),
    ("Samsung R&D India", ["Software Engineer", "Research Engineer"]),
    ("Qualcomm India", ["Software Engineer", "Embedded Engineer"]),
    ("Intel India", ["Software Engineer", "Validation Engineer"]),
    ("Cisco India", ["Software Engineer", "Network Engineer"]),
    ("Zoho", ["Member Technical Staff", "Software Engineer"]),
    ("Freshworks", ["Software Engineer", "Backend Engineer"]),
    ("InMobi", ["Data Engineer", "Software Engineer"]),
    ("MakeMyTrip", ["Software Engineer", "QA Engineer"]),
    ("Myntra", ["Software Engineer", "Frontend Engineer"]),
    ("Reliance Jio", ["Software Engineer", "Network Engineer"]),
    ("L&T Technology Services", ["Engineer Trainee", "Software Engineer"]),
]

ROUNDS = [
    "OA + 2 Technical + HR",
    "Aptitude + Coding + Technical + HR",
    "Coding Test + Technical + Managerial + HR",
    "OA + Technical + HR",
    "OA + Technical + System Design + HR",
    "Group Discussion + Technical + HR",
]

DIFFICULTIES = ["Easy", "Medium", "Hard"]

PROJECTS = [
    "a resume parser using NLP",
    "a campus navigation app with React Native",
    "an e-commerce backend using Spring Boot",
    "a placement tracker built with Flask",
    "a personal finance dashboard in React",
    "a movie recommendation system using collaborative filtering",
    "a smart attendance system with face recognition",
    "a hostel management portal",
    "a food delivery clone API",
]

TOPIC_SENTENCES = {
    "DSA": "DSA questions covered arrays, binary search, graphs, and dynamic programming.",
    "DBMS": "DBMS discussion included SQL joins, normalization, indexes, and transactions.",
    "OS": "OS concepts like process scheduling, threads, and deadlock were asked.",
    "CN": "CN topics covered TCP vs UDP, HTTP, and DNS latency.",
    "OOP": "OOP fundamentals like classes, objects, inheritance, polymorphism, and encapsulation were discussed.",
    "HR": "HR questions included tell me about yourself, strengths/weakness, and a team conflict.",
}

DSA_QUESTIONS = [
    "Find the longest subarray with sum k in an array?",
    "Implement LRU cache using a hash map and doubly linked list?",
    "Detect a cycle in a linked list and explain complexity?",
    "Shortest path in a weighted graph using Dijkstra?",
    "DP on grid for minimum path sum?",
    "Binary search on answer for allocation problem?",
]

DBMS_QUESTIONS = [
    "What is normalization? Explain 1NF, 2NF, 3NF?",
    "Write SQL to fetch the second highest salary?",
    "Difference between clustered and non-clustered index?",
    "Explain ACID properties and transactions?",
    "Explain inner join vs left join in SQL?",
]

OS_QUESTIONS = [
    "What is a process vs thread?",
    "Explain deadlock and prevention?",
    "CPU scheduling algorithms like RR and SJF?",
    "Explain paging and virtual memory?",
]

CN_QUESTIONS = [
    "TCP vs UDP differences?",
    "Explain HTTP vs HTTPS handshake?",
    "DNS resolution flow?",
    "Explain the TCP three-way handshake?",
]

OOP_QUESTIONS = [
    "Explain OOP pillars: encapsulation, inheritance, polymorphism, abstraction?",
    "Difference between interface and abstract class?",
    "Explain method overloading vs overriding?",
    "Design a class for a parking lot?",
]

HR_QUESTIONS = [
    "Tell me about yourself?",
    "Why this company?",
    "Describe a conflict in a team project?",
    "What are your strengths and weaknesses?",
    "Where do you see yourself in 3 years?",
]

INTRO_TEMPLATES = [
    "I am {name} from {college}. I sat for {company} as a {role} through campus placements in {year}.",
    "{company} visited our campus in {year} for the {role} role. I'm {name} from {college}.",
    "My {year} campus interview at {company} for {role} started with multiple rounds. I am {name} from {college}.",
]

OA_TEMPLATES = [
    "Online test had aptitude, basic DSA, and SQL MCQs. The coding question was on {dsa_focus}.",
    "OA included two coding problems and a short DBMS quiz on normalization and indexes.",
    "First round was a timed coding test with arrays and binary search plus a CN section on TCP/HTTP.",
]

TECH_TEMPLATES = [
    "Technical round focused on {dsa_focus} and OOP fundamentals like inheritance and polymorphism.",
    "They asked about OS process scheduling and deadlock, then a coding problem on {dsa_focus}.",
    "Interviewers went deep into DBMS transactions, SQL joins, and indexing, followed by a graph problem.",
    "A system design discussion covered REST APIs, caching, and network latency.",
]

HR_TEMPLATES = [
    "HR round asked, 'Tell me about yourself?' and 'Why {company}?'.",
    "They asked about strengths/weakness, a team conflict, and career goals.",
    "HR was friendly with questions about leadership, challenges, and culture fit.",
]

TIPS = [
    "Revise DSA patterns, DBMS basics, and keep a project story ready.",
    "Practice arrays, graphs, and be crisp with SQL queries.",
    "Prepare OS and CN fundamentals along with OOP concepts.",
    "Communicate your approach clearly and analyze time complexity.",
]

TOPIC_COMBOS = [
    ["DSA", "DBMS", "HR"],
    ["DSA", "OS", "HR"],
    ["DSA", "CN", "HR"],
    ["DSA", "OOP", "HR"],
    ["DSA", "DBMS", "OS", "HR"],
    ["DSA", "DBMS", "CN"],
    ["DBMS", "OOP", "HR"],
    ["DSA", "OS", "CN"],
    ["DSA", "DBMS", "OOP", "HR"],
    ["DSA", "CN", "OOP"],
    ["DSA", "DBMS", "OS"],
    ["DSA", "HR"],
    ["DSA", "DBMS"],
    ["DBMS", "CN", "HR"],
]


def _pick_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _pick_questions(topics: List[str], rng: random.Random) -> List[str]:
    question_pool = []
    if "DSA" in topics:
        question_pool.append(rng.choice(DSA_QUESTIONS))
    if "DBMS" in topics:
        question_pool.append(rng.choice(DBMS_QUESTIONS))
    if "OS" in topics:
        question_pool.append(rng.choice(OS_QUESTIONS))
    if "CN" in topics:
        question_pool.append(rng.choice(CN_QUESTIONS))
    if "OOP" in topics:
        question_pool.append(rng.choice(OOP_QUESTIONS))
    if "HR" in topics:
        question_pool.append(rng.choice(HR_QUESTIONS))

    while len(question_pool) < 4:
        question_pool.append(rng.choice(DSA_QUESTIONS))

    return question_pool[:4]


def _build_raw_text(record: "SeedRecord", rng: random.Random) -> str:
    intro = rng.choice(INTRO_TEMPLATES).format(
        name=record.name, college=record.college, company=record.company, role=record.role, year=record.year
    )
    oa = rng.choice(OA_TEMPLATES).format(dsa_focus=record.dsa_focus)
    tech = rng.choice(TECH_TEMPLATES).format(dsa_focus=record.dsa_focus)
    hr = rng.choice(HR_TEMPLATES).format(company=record.company)
    project = rng.choice(PROJECTS)

    lines = [
        intro,
        f"Rounds: {record.rounds}.",
        f"Round 1 (OA): {oa}",
        f"Round 2 (Technical): {tech}",
        f"Round 3 (HR): {hr}",
    ]

    for topic in record.topics:
        lines.append(TOPIC_SENTENCES[topic])

    lines.append(f"Project discussion: I talked about {project} and the tech stack choices.")
    lines.append("Questions asked:")
    for idx, question in enumerate(record.questions, start=1):
        lines.append(f"Q{idx}: {question}")
    lines.append(f"Overall difficulty felt {record.difficulty}.")
    lines.append(f"Tips: {rng.choice(TIPS)}")

    return "\n".join(lines)


@dataclass
class SeedRecord:
    doc_id: str
    name: str
    college: str
    company: str
    role: str
    year: int
    rounds: str
    difficulty: str
    topics: List[str]
    dsa_focus: str
    questions: List[str]
    raw_text: str


def generate_seed_records(count: int, rng: random.Random) -> List[SeedRecord]:
    records: List[SeedRecord] = []
    dsa_focus_options = [
        "graphs and BFS",
        "dynamic programming on grids",
        "binary search on answer",
        "arrays and hashing",
        "trees and recursion",
        "stack and queue applications",
    ]

    for idx in range(count):
        company, roles = rng.choice(COMPANIES)
        role = rng.choice(roles)
        year = rng.choice([2019, 2020, 2021, 2022, 2023, 2024, 2025])
        topics = rng.choice(TOPIC_COMBOS).copy()
        if "DSA" not in topics:
            topics.insert(0, "DSA")
        topics = list(dict.fromkeys(topics))

        record = SeedRecord(
            doc_id=f"seed_{idx:03d}",
            name=_pick_name(rng),
            college=rng.choice(COLLEGES),
            company=company,
            role=role,
            year=year,
            rounds=rng.choice(ROUNDS),
            difficulty=rng.choice(DIFFICULTIES),
            topics=topics,
            dsa_focus=rng.choice(dsa_focus_options),
            questions=[],
            raw_text="",
        )

        record.questions = _pick_questions(record.topics, rng)
        record.raw_text = _build_raw_text(record, rng)
        records.append(record)

    return records


def ensure_seeded(count: int = SEED_COUNT) -> dict:
    meta_ref = db.collection("metadata").document(f"seed_{SEED_VERSION}")
    meta_snapshot = meta_ref.get()
    if meta_snapshot.exists:
        meta = meta_snapshot.to_dict() or {}
        if meta.get("seeded"):
            return {"seeded": True, "count": meta.get("count", 0)}

    seed_user = {
        "uid": SEED_UID,
        "name": "Aarav Sharma",
        "email": "aarav.sharma+seed@placementarchive.local",
        "role": "contributor",
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection("users").document(SEED_UID).set(seed_user, merge=True)

    rng = random.Random(42)
    records = generate_seed_records(count, rng)

    created = 0
    for record in records:
        doc_ref = db.collection("interview_experiences").document(record.doc_id)
        snapshot = doc_ref.get()
        if snapshot.exists:
            existing = snapshot.to_dict() or {}
            if existing.get("embedding_id") is not None:
                continue

        processed = pipeline.process(record.raw_text)
        topics = processed["topics"] or record.topics
        embedding_id = faiss_store.add_vector(processed["embedding"], doc_ref.id)

        doc_ref.set(
            {
                "company": record.company,
                "role": record.role,
                "year": record.year,
                "round": record.rounds,
                "difficulty": record.difficulty,
                "raw_text": record.raw_text,
                "extracted_questions": processed["questions"],
                "topics": topics,
                "summary": processed["summary"],
                "embedding_id": embedding_id,
                "created_by": SEED_UID,
                "created_at": firestore.SERVER_TIMESTAMP,
                "is_active": True,
                "is_anonymous": False,
                "edit_history": [],
            },
            merge=True,
        )
        created += 1

    meta_ref.set(
        {
            "seeded": True,
            "count": count,
            "created": created,
            "version": SEED_VERSION,
            "seeded_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {"seeded": True, "count": count, "created": created}
