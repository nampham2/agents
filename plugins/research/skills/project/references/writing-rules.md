# Writing rules for project records

Write the specification, architecture, task fields, handoff and notes with these 45 rules.
They are the project's own rules, inspired by ASD-STE100. Thirteen come from the public list of
STE rules and are marked (D). The other 32 are marked (I). Do not claim that a record follows STE.

`research-validate` reports five kinds of finding as advice. It checks form only. Quoted text,
code, tables and headings are exempt. Preferred words are in
[the word table](writing-rules-extra.md).

## Words

1. (D) Use each word in one meaning and one part of speech.
2. (I) Use the same word for the same thing every time. Do not vary words for style.
3. (I) Use the glossary term for a project concept.
4. (I) Use the preferred word. A finding prints it.
5. (I) Use plain words. Do not use idioms or slang.
6. (I) Do not use a phrasal verb with several meanings. Write `start`, not `kick off`.
7. (I) Spell out an abbreviation at its first use.
8. (I) Write numbers as digits.
9. (I) Write `for example`, not `e.g.`.
10. (I) Write the full name of a file or command in code format.

## Noun clusters

11. (D) Do not write more than three nouns in a row.
12. (I) Split a long cluster with `of`, `for` or `in`.
13. (I) Define a long cluster once in the glossary. Then use the short term.

## Verbs

14. (D) Use only these verb forms: infinitive, imperative, simple present, simple past, simple
    future, and the past participle as an adjective.
15. (D) Do not build a verb phrase with auxiliary verbs.
16. (D) Use the `-ing` form only as a name or inside a name.
17. (D) Use the active voice. Use the passive voice in a description only when the actor is unknown.
18. (I) Name who does each action.
19. (I) Use the simple present for what the tool does. Do not write `will`, `should` or `could`.
20. (I) Use `must` only for a requirement.
21. (I) Use a plain verb. Write `check`, not `do a check`.

## Sentences

22. (D) Write up to 20 words in an instruction and up to 25 words in any other sentence.
23. (D) Keep the subject, the verb and the article in every sentence.
24. (I) Put one idea in each sentence.
25. (I) Put a condition first: `If the test fails, stop.`
26. (I) Put the main action early in the sentence.
27. (I) Do not use contractions. Write `do not`, not `don't`.
28. (I) Do not join long clauses with `and` or `but`. Write two sentences.
29. (I) Do not put a long remark in brackets. Write it as its own sentence.

## Procedures

30. (D) Write one instruction in each sentence.
31. (D) Make each instruction clear and specific. Name the file, the command or the value.
32. (I) Start each step with a verb.
33. (I) Write the result of a step as a separate sentence after the step.
34. (I) Write a requirement before the step that needs it.
35. (I) Write the steps in the order that you do them. Number them.

## Descriptions

36. (D) Write one topic in each paragraph.
37. (D) Write up to six sentences in a paragraph.
38. (D) Use a vertical list for complex text, such as three or more parallel items.
39. (I) Start a paragraph with its main point.
40. (I) Put the reason for a decision next to the decision.
41. (I) Use a table only for data with columns.

## Punctuation and layout

42. (I) End each sentence with a period.
43. (I) Do not use a semicolon. Write two sentences.
44. (I) Keep the items of a list short and parallel.
45. (I) Keep a heading short. Put each fact in the section where a reader looks for it.

## Glossary

Each term has one meaning in a record.

- **project**: one folder in the workspace with its records.
- **record**: a file or field in a project that holds a fact.
- **living record**: a record that agents keep current, such as `architecture.md`.
- **finding**: a note about something that you saw, saved in a task note.
- **evidence**: the saved output of a command, kept in `evidence.md`.
- **gate**: a rule in the tool that refuses a step.
- **worktree**: a Git checkout that the project records.
- **checkpoint**: a saved handoff with its revision and tokens.
- **agreement**: the user's confirmation of a proposal, saved with the proposal hash.
- **cutoff**: the time after which a rule applies to new projects.
