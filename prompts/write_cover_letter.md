---
version: 1
description: Profile + job offer -> CoverLetter JSON.
---
You write a cover letter for a fresh engineering graduate applying to a job in Singapore.
You only use facts from the candidate's knowledge base and from the offer.

Return only a JSON object that matches this JSON schema:

<schema>
$schema
</schema>

Structure (3 or 4 paragraphs, $min_words to $max_words words in total, English):
1. A specific opening: the role, and what draws the candidate to this company, using only
   what the offer says about the company and its work. No generic praise.
2. One or two paragraphs with two concrete proofs from the knowledge base, each linked to a
   need stated in the offer (a responsibility or a required skill).
3. Why Singapore, built on the knowledge base's "motivations" entries.
4. A short conclusion asking for an interview.

Rules:
- source_ids lists the ids of every knowledge-base element a paragraph uses (bullets,
  experiences, projects, skills, motivations). The paragraph about Singapore cites at least one
  motivations id.
- Numbers, technologies, companies and schools must come from the cited elements or from the
  offer. Never invent results, tools or experience.
- Direct, confident tone. No clichés ("I am writing to express my interest", "team player",
  "passionate", "dynamic"), no flattery, no exclamation marks.
- greeting: "Dear Hiring Manager," unless the offer names a contact person.
- subject: "Application for <job title> – <candidate name>".
- closing: a short sign-off such as "Kind regards," (the name is added automatically).
- Never mention age, nationality or visa status.

Candidate name: $candidate_name

<knowledge_base>
$profile
</knowledge_base>

<offer>
$offer
</offer>
