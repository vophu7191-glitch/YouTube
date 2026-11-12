---
title: Jion Privacy Policy
---

# Privacy Policy for Jion

**Effective date: 12 November 2026**

This Privacy Policy explains what information the Jion Discord bot processes, why it processes it, and how users can ask questions or request deletion.

## 1. What Jion does

Jion is a Discord bot that helps server communities monitor YouTube livestream donations and optionally verify the connection between a Discord account and a YouTube channel.

## 2. Information processed

Depending on the features used, Jion may process:

- Discord user ID and server (guild) ID.
- YouTube channel ID and channel display name supplied during verification.
- YouTube livestream and donation event information made available through the YouTube API, such as the channel, event amount, currency, public comment, and event time.
- Temporary verification records, including a random request/state identifier and expiration time.
- Technical information required to operate the bot and respond to service errors.

When a member posts a supported YouTube channel URL for verification, Jion reads the message content to extract the channel URL. The verification flow does not intentionally store the full original message text. The original Discord message is scheduled for deletion after five minutes; deletion can fail if the bot lacks permission or is offline.

## 3. Why information is used

Information is used only to:

- Identify the YouTube channel a member chose to submit.
- Verify channel management through Google OAuth and the YouTube Data API.
- Associate a verified YouTube channel ID with the relevant Discord user ID.
- Process and display YouTube livestream donation events for servers that configure the feature.
- Prevent duplicate records, maintain service operation, and troubleshoot errors.

Jion does not use Discord message content to train AI or machine-learning models.

## 4. Google authorization

If a user chooses to verify a channel, Google sign-in and authorization are handled by Google. Jion requests the YouTube read-only permission needed to check which channel or channels the signed-in Google account manages.

Jion uses the API response to compare channel IDs. Jion does not need the user's Google password. OAuth access credentials are used to complete the verification request and are not intended to be stored as a long-term account credential by Jion.

Google's handling of information is governed by Google's own privacy policy and terms.

## 5. Storage and service providers

Jion stores operational records in MongoDB. The bot is hosted on Render. Discord and Google provide the platform and API services used by the bot.

These providers may process information as necessary to provide their services and under their own terms and privacy policies. Jion's operator does not control their independent practices.

## 6. Retention

Temporary verification requests and OAuth state records are created with an expiration time of approximately ten minutes and are configured for automatic expiry in MongoDB.

A successful Discord-to-YouTube channel association may remain stored so that the bot can recognize the verified channel in later activity. Donation and related server records may also remain stored for the bot's features. They are not automatically covered by the ten-minute verification expiry.

Users who want an association or other bot data reviewed or deleted can contact the bot operator through the repository's issue page: https://github.com/vophu7191-glitch/YouTube/issues. Do not post private account details, OAuth codes, tokens, or secrets in a public issue. The operator may need enough information to identify the relevant Discord account or server before processing a request.

## 7. Sharing

Jion does not sell personal information. Information is processed by Discord, Google, MongoDB, and Render as needed to operate the bot and its integrations. Information may also be disclosed if required by applicable law or to protect the service and its users.

## 8. User choices

Using the Google verification flow is optional. A member can choose not to submit a channel URL or not to continue with Google authorization. Users can contact the operator to request review or deletion of a stored channel association.

## 9. Security

The operator uses reasonable measures intended to protect configuration secrets and stored records. No online service or data transmission can be guaranteed to be completely secure. Users should never share passwords, OAuth codes, access tokens, or other secrets with the bot or in public support channels.

## 10. Changes to this policy

This policy may be updated when Jion's features or data practices change. The effective date at the top will be updated when material changes are made.

## 11. Contact

For privacy questions or data requests, contact the Jion operator through: https://github.com/vophu7191-glitch/YouTube/issues

Please avoid including sensitive information in public issues.