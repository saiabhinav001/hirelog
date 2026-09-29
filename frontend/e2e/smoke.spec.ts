import { expect, test } from "@playwright/test";

type PracticeListPayload = {
  id: string;
  name: string;
  user_id: string;
  created_at: string;
  question_count: number;
  revised_count: number;
  practicing_count: number;
  unvisited_count: number;
  topic_distribution: Record<string, number>;
  revised_percent: number;
};

test("smoke: signup, submit, search, save-to-list, dashboard", async ({ page }) => {
  const createdAt = new Date().toISOString();
  const practiceLists: PracticeListPayload[] = [
    {
      id: "list-1",
      name: "Core DSA",
      user_id: "e2e-user",
      created_at: createdAt,
      question_count: 0,
      revised_count: 0,
      practicing_count: 0,
      unvisited_count: 0,
      topic_distribution: { DSA: 1 },
      revised_percent: 0,
    },
  ];

  let submittedCompany = "";
  let submittedQuestions: string[] = [];

  await page.route("**/api/search/facets", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_at: createdAt,
        top_topics: ["DSA", "System Design"],
        top_companies: ["OpenAI", "Google"],
      }),
    });
  });

  await page.route("**/api/experiences", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }

    const payloadText = route.request().postData() ?? "{}";
    const payload = JSON.parse(payloadText) as {
      company?: string;
      user_questions?: string[];
    };
    submittedCompany = payload.company ?? "";
    submittedQuestions = payload.user_questions ?? [];

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ id: "exp-1", status: "ok" }),
    });
  });

  await page.route("**/api/search?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        results: [
          {
            id: "exp-1",
            company: "OpenAI",
            role: "Software Intern",
            year: 2024,
            round: "Technical Round 1",
            difficulty: "Medium",
            raw_text: "Graph and system design interview",
            extracted_questions: [
              {
                question_text: "Explain quicksort.",
                question: "Explain quicksort.",
                topic: "DSA",
                category: "Algorithms",
                confidence: 1,
                source: "user",
              },
              {
                question_text: "How do you design an LRU cache?",
                question: "How do you design an LRU cache?",
                topic: "System Design",
                category: "Design",
                confidence: 0.84,
                source: "ai",
              },
            ],
            topics: ["DSA", "System Design"],
            summary: "Balanced DSA + design process.",
            created_by: "e2e-user",
            contributor_display: "E2E User",
            nlp_status: "done",
            stats: {
              user_question_count: 1,
              extracted_question_count: 1,
              total_question_count: 2,
            },
          },
        ],
        total: 1,
        total_count: 1,
        returned_count: 1,
        has_more: false,
        next_cursor: null,
        served_mode: "hybrid",
        served_engine: "faiss",
      }),
    });
  });

  await page.route("**/api/practice-lists", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(practiceLists),
      });
      return;
    }

    if (route.request().method() === "POST") {
      const payloadText = route.request().postData() ?? "{}";
      const payload = JSON.parse(payloadText) as { name?: string };
      const newList: PracticeListPayload = {
        id: `list-${practiceLists.length + 1}`,
        name: payload.name ?? "New List",
        user_id: "e2e-user",
        created_at: createdAt,
        question_count: 0,
        revised_count: 0,
        practicing_count: 0,
        unvisited_count: 0,
        topic_distribution: {},
        revised_percent: 0,
      };
      practiceLists.unshift(newList);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(newList),
      });
      return;
    }

    await route.fallback();
  });

  await page.route("**/api/practice-lists/**/questions", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ id: "pq-1", status: "saved" }),
    });
  });

  await page.route("**/api/dashboard/stats", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total_experiences: 24,
        top_company: "OpenAI",
        top_topic: "DSA",
        data_freshness: {
          generated_at: createdAt,
          freshness_seconds: 30,
        },
        contribution_impact: {
          experiences_submitted: 3,
          questions_extracted: 12,
          archive_size: 24,
        },
      }),
    });
  });

  await page.route("**/api/dashboard/charts", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        topic_totals: { DSA: 10, "System Design": 6 },
        difficulty_distribution: { Easy: 3, Medium: 12, Hard: 9 },
        company_topic_counts: { OpenAI: { DSA: 4 } },
        insights: ["DSA appears most frequently in recent rounds."],
      }),
    });
  });

  await page.route("**/api/dashboard/questions?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        frequent_questions: {
          "Explain quicksort.": 4,
          "Design an LRU cache.": 3,
        },
      }),
    });
  });

  await page.route("**/api/dashboard/flows?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        interview_progression: {},
      }),
    });
  });

  await page.goto("/signup");
  await page.getByLabel("Full name").fill("E2E User");
  await page.getByLabel("Email address").fill("e2e@example.edu");
  await page.locator("#signup-password").fill("password123");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/search$/);

  await page.goto("/submit");
  await page.getByPlaceholder("e.g. Google").fill("OpenAI");
  await page.getByPlaceholder("e.g. SDE Intern").fill("Software Intern");
  await page.getByPlaceholder("e.g. Technical Round 1").fill("Technical Round 1");
  await page.getByPlaceholder("Describe your interview experience...").fill("Focused on graph traversal and caching design.");
  await page.getByPlaceholder("What is the time complexity of quicksort?\nExplain ACID properties in DBMS.\nHow does virtual memory work?").fill("Explain quicksort.\nDesign an LRU cache.");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page).toHaveURL(/\/contributions$/);
  expect(submittedCompany).toBe("OpenAI");
  expect(submittedQuestions).toEqual(["Explain quicksort.", "Design an LRU cache."]);

  await page.goto("/search");
  await page.getByLabel("Search query").fill("graphs and caching");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/\/results\?/);
  await expect(page.getByText("OpenAI")).toBeVisible();

  await page.getByRole("button", { name: /Save question to practice list/ }).first().click();
  await page.getByRole("button", { name: "Save question to list Core DSA" }).click();
  await expect(page.getByText("Added to 'Core DSA'.")).toBeVisible();

  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { name: "Placement Analytics" })).toBeVisible();
  await expect(page.getByText("Top repeated questions")).toBeVisible();
});
