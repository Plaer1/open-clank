# Resolve the recipient before sending

- Mail account authorization is separate from application and model-provider login. A configured provider does not establish that every mail provider is available.
- Drafting and sending are different actions. Check recipient identity, attachment and explicit send authorization.

**Offline account:** practice@lesson.invalid. Contacts: C1 Alex Design, alex.design@lesson.invalid; C2 Alex Finance, alex.finance@lesson.invalid. Request: `Ask Alex to review.` Draft subject: Draft review. Body: `Please review the attached draft by October 20.` Attachment review.txt contains `Status: draft\n`. Google mail is configured; Microsoft OAuth/Graph is unavailable.

Resolve which Alex is intended before addressing or sending. This exercise prepares a draft only; all addresses are fictional.

A send-submission receipt would need the intended account, resolved recipient, message identity and successful submission state. It still would not establish that the human recipient read the message.
