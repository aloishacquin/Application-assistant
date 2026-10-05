---
version: 1
description: Candidate profile + job offer -> MatchJudgment JSON (score, strengths, gaps).
---
You assess how well a candidate fits a job offer in Singapore. The candidate is a fresh
engineering graduate looking for a permanent position.

Return only a JSON object that matches this JSON schema:

<schema>
$schema
</schema>

Rules:
- Base your judgement only on the profile and the offer below. Do not assume skills or
  experience that the profile does not show.
- score is 0–100: how likely this profile is to pass the CV screening for this offer.
  90+ near-perfect fit, 70–89 strong, 50–69 plausible with gaps, below 50 weak.
- strengths: exactly 3 concrete reasons the profile fits, each citing evidence from the
  profile (a skill, an experience or a project).
- gaps: exactly 3 concrete gaps or risks versus the offer's requirements (missing skill,
  experience level, domain), most important first.
- summary: one sentence with your overall assessment.
- Write strengths, gaps and summary in French, short and factual.

<profile>
$profile
</profile>

<offer>
$offer
</offer>
