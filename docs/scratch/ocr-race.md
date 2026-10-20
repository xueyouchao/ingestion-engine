# Scratch: OCR concurrency race test

Throwaway file. It exists only so a push and a `/open-code-review` comment can
be fired a few seconds apart, which is the race that used to cancel one of the
two review runs. This branch is deleted after the test.

## What is being checked

That the automatic review of a push and a manual review of the same head can
complete at the same time instead of cancelling each other.
