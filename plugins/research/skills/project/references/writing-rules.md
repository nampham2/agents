# Writing rules for project records

Write the specification, planning, task fields, handoff and notes with these 45 rules.
They are inspired by ASD-STE100. Thirteen come from the public list (D).
The other 32 are marked (I) as project rules. Do not claim that a record follows STE.

`research-validate` reports five kinds of finding as advice. It checks form only and skips quoted
text, code, tables and headings. Preferred words are in
[the word table](writing-rules-extra.md).

## Words

1. (D) Use each word in one meaning and part of speech.
2. (I) Use the same word for the same thing. Do not vary words for style.
3. (I) Use the glossary term for a concept.
4. (I) Use the preferred word that a finding prints.
5. (I) Do not use idioms or slang.
6. (I) Write `start`, not `kick off`.
7. (I) Spell out an abbreviation once.
8. (I) Use digits for numbers.
9. (I) Write `for example`, not `e.g.`.
10. (I) Put a file or command name in code format.

## Noun clusters

11. (D) Write up to three nouns in a row.
12. (I) Split a cluster with `of`, `for` or `in`.
13. (I) Define a long cluster in the glossary. Then use the short term.

## Verbs

14. (D) Use only the infinitive, imperative, simple present, simple past, simple future, and the
    past participle as an adjective.
15. (D) Do not use auxiliary verbs.
16. (D) Use `-ing` only in a name.
17. (D) Use the active voice. Use the passive voice only when the actor is unknown.
18. (I) Name who does the action.
19. (I) Do not write `will`, `should` or `could`. Use the simple present.
20. (I) Use `must` for a requirement only.
21. (I) Write `check`, not `do a check`.

## Sentences

22. (D) Write up to 20 words in an instruction and 25 in any other sentence.
23. (D) Keep the subject, verb and article.
24. (I) Put one idea in a sentence.
25. (I) Put a condition first: `If it fails, stop.`
26. (I) Put the main action early.
27. (I) Do not use contractions: write `do not`.
28. (I) Do not join long clauses with `and` or `but`.
29. (I) Do not put a long remark in brackets.

## Procedures

30. (D) Write one instruction in a sentence.
31. (D) Make each instruction specific. Name the file, command or value.
32. (I) Start a step with a verb.
33. (I) Write the result of a step as its own sentence.
34. (I) Write a requirement before its step.
35. (I) Number the steps in the order that you do them.

## Descriptions

36. (D) Write one topic in a paragraph.
37. (D) Write up to six sentences in a paragraph.
38. (D) Use a vertical list for three or more parallel items.
39. (I) Start a paragraph with its point.
40. (I) Put the reason beside the decision.
41. (I) Use a table only for columns of data.

## Punctuation and layout

42. (I) End a sentence with a period.
43. (I) Do not use a semicolon.
44. (I) Keep list items short and parallel.
45. (I) Keep a heading short. Put a fact where a reader looks for it.

## Glossary

Each term has one meaning.

- **project**: one workspace folder with its records.
- **record**: a file or field that holds a fact.
- **living record**: a record that agents keep current, like `planning.md`.
- **finding**: something that you saw, saved in a task note.
- **evidence**: saved command output, kept in `evidence.md`.
- **gate**: a tool rule that refuses a step.
- **worktree**: a Git checkout that the project records.
- **checkpoint**: a saved handoff with revision and tokens.
- **agreement**: the user's confirmation of a proposal, saved with its hash.
- **cutoff**: the time after which a rule applies to new projects.
