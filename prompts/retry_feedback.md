---
version: 1
description: Appended to a prompt when the previous answer failed validation.
---
$prompt

<previous_answer>
$previous_answer
</previous_answer>

Your previous answer was rejected for these reasons:
$errors

Return a corrected JSON object that fixes every error above.
