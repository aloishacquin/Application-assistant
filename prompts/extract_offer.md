---
version: 2
description: Raw job offer text -> JobOfferExtraction JSON.
---
You extract structured data from a job offer for a candidate applying to jobs in Singapore.

Return only a JSON object that matches this JSON schema:

<schema>
$schema
</schema>

Rules:
- Use only information present in the offer. Never guess or infer missing facts.
- Salaries are monthly amounts in SGD:
  - an annual salary is divided by 12 and rounded to the nearest integer;
  - a single figure sets both salary_min_sgd and salary_max_sgd;
  - "up to X" sets only salary_max_sgd, "from X" only salary_min_sgd;
  - if no salary is given, or it is in a currency other than SGD, both fields are null.
- Bonuses, equity and allowances are not part of the salary.
- sector is "financial_services" only for banks, insurers, asset managers, payment and
  trading firms; otherwise "other".
- required_skills (presented as mandatory) and nice_to_have (presented as optional or a plus)
  contain only skills a candidate can list on a CV: programming languages, tools, frameworks,
  platforms, technical methods, domain knowledge, and required spoken languages.
  - Use the short name of each skill: "Airflow", not "Orchestration tool (Airflow preferred)";
    "Kafka", not "Kafka or other streaming technologies".
  - When the offer allows alternatives ("Power BI or Tableau"), list each one.
  - Never include degrees, years of experience, or soft skills (communication, teamwork,
    autonomy). A requirement such as "Good communication in English" becomes "English".
- min_years_experience is the minimum number of years of experience required:
  - "0–2 years" gives 0; "3+ years" gives 3;
  - 0 when the offer says fresh graduates are welcome or no experience is required;
  - null when the offer does not state it. Do not infer it from the job title or seniority.
- keywords are technologies, tools, methods and domain terms copied verbatim from the offer,
  as an applicant tracking system would match them. No duplicates.
- seniority is "unknown" if the offer does not state or clearly imply it. For a range such as
  "Junior to Mid-level", use the lower bound.
- contact_email is null unless an email address appears in the offer.
- language is the ISO 639-1 code of the language the offer is written in.

<offer>
$offer_text
</offer>
