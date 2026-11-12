---
title: Jion Privacy Policy
---

# Jion Privacy Policy

**Effective date: 12 November 2026**

This policy explains what information the Jion Discord bot processes, why it is processed, where it is stored, and how users can request deletion.

## 1. About Jion

Jion is a Discord bot that provides YouTube-related community features. Depending on a server's configuration, these features include linking a Discord account to a YouTube channel, monitoring selected YouTube livestreams, and displaying livestream donation and gift events in Discord.

## 2. Information Jion processes

Depending on which features are used, Jion may process:

- **Discord identifiers:** Discord user IDs, server (guild) IDs, and Discord channel IDs used for configuration and announcements.
- **YouTube channel information:** channel IDs, public channel names, handles, and public profile image URLs returned by the YouTube Data API.
- **Verification information:** a temporary one-time verification code, the Discord user ID and server ID associated with the request, the submitted YouTube channel ID, and the code's expiration time.
- **Livestream and donation information:** YouTube video and channel IDs, publicly displayed creator names, event IDs, donation or gift amounts, currency, public donor comments or gift labels, and event timestamps.
- **Server configuration and moderation workflow records:** selected YouTube channels, announcement channel settings, and limited records used by server administrators to review whether members have linked a channel.
- **Operational logs:** technical events and error details needed to run, secure, and troubleshoot the bot.

## 3. Message Content Intent and Discord messages

Jion uses Discord's Message Content Intent to detect YouTube channel links that members voluntarily post in a server where the bot is present. The bot reads the message content to identify a supported YouTube channel URL and start the channel-link verification flow.

Jion does not intentionally save the full text of these submitted Discord messages in its database. After starting the verification flow, the bot schedules the original message for deletion after five minutes. Deletion may fail if Discord permissions do not allow it, the message is already unavailable, or the bot is offline.

Jion does not need to read unrelated conversations for advertising or profiling. Message content is used for the YouTube-link detection feature.

## 4. YouTube channel verification

A member can submit a YouTube channel URL or use the bot's linking command. Jion generates a temporary code and asks the member to place it in the public description of the correct YouTube channel. When the member asks Jion to verify, Jion uses the YouTube Data API to read that channel's public description and check for the code.

The code is designed for one-time use and expires after approximately ten minutes. Once verified, Jion stores the association between the Discord user ID and YouTube channel ID so the channel can be recognized by the bot later.

This process does **not** use Google OAuth sign-in and does not ask for a Google password or access to a Google account. It verifies that the person can edit the submitted channel description at the time of verification; it is not a legal identity check.

## 5. Livestream monitoring and donation events

When a server configures YouTube channels for monitoring, Jion checks for livestreams and reads the relevant YouTube live chat data needed for its features. It checks chat messages for supported linking codes and processes public Super Chat, Super Sticker, and gift events.

For supported donation and gift events, Jion may store the event identifier, YouTube channel and video IDs, displayed amount, currency or gift information, public comment or gift label, server ID, and event time. It may display relevant event information in the Discord announcement channel configured by that server.

Jion does not intentionally store ordinary YouTube live chat messages as a chat archive.

## 6. How information is used

Information is used to:

- Detect YouTube channel links submitted to the bot.
- Verify control of a YouTube channel through a temporary description code.
- Maintain the Discord-to-YouTube channel association.
- Monitor livestreams selected by a server.
- Record and display supported donation and gift events, and provide related server features such as donation summaries or rankings.
- Prevent duplicate event records, maintain server configuration, and troubleshoot service errors.

Jion does not sell personal information or use message content to train AI models.

## 7. Storage, hosting, and third-party services

Jion stores its application records in MongoDB. The bot is hosted on Render. It communicates with Discord to provide bot functionality and with the YouTube Data API and YouTube services to retrieve channel and livestream information.

These providers process information under their own terms and privacy practices. Jion's operator does not control the independent data practices of these providers.

Relevant policies:
- Discord Privacy Policy: https://discord.com/privacy
- Google Privacy Policy: https://policies.google.com/privacy
- YouTube Terms of Service: https://www.youtube.com/t/terms

## 8. Retention and deletion

Temporary verification records are configured to expire after approximately ten minutes. The original Discord message submitted for automatic link detection is scheduled for deletion after five minutes, subject to the limitations described above.

A successfully verified Discord-to-YouTube association remains in the database until it is unlinked or deleted. A user can use the bot's `/unlink` command to remove their own stored channel association. A server administrator with the required permission can use `/huylienket` to remove a member's association.

Unlinking removes the association; it does not automatically erase historical donation, gift, livestream, or server records that may be needed for the bot's features. To request review or deletion of other stored records, contact the operator using the private contact method provided by the server or bot operator.

## 9. Sharing and disclosure

Jion does not sell personal information. Information may be processed by Discord, YouTube/Google, MongoDB, and Render as necessary to operate the service. Event information may be posted to the Discord channels configured by a server. Information may also be disclosed where required by applicable law or where reasonably necessary to protect the service and its users.

## 10. Security

The operator takes reasonable steps to protect bot credentials and stored records. No online service can guarantee absolute security. Users should never send passwords, bot tokens, API keys, or other authentication secrets to Jion or post them in public channels.

## 11. Children's privacy

Jion is a community tool and is not designed to collect sensitive personal information from children. Server owners and users should follow Discord's age requirements and applicable laws. Do not submit sensitive personal information through the bot.

## 12. Changes to this policy

This policy may be updated when Jion's features or data practices change. The effective date at the top will be revised when material changes are made.

## 13. Contact and privacy requests

For privacy questions or requests to access, correct, or delete information, contact the Jion operator through this private contact method:

**[ADD A PRIVATE CONTACT EMAIL OR SUPPORT METHOD BEFORE PUBLISHING]**

Do not include passwords, verification codes, bot tokens, API keys, or other secrets in a privacy request.
 
---

# Chính sách quyền riêng tư của Jion

**Ngày có hiệu lực: 12/11/2026**

Chính sách này giải thích bot Discord Jion xử lý thông tin nào, vì sao xử lý, lưu trữ ở đâu và cách người dùng yêu cầu xóa dữ liệu.

## 1. Giới thiệu về Jion

Jion là bot Discord cung cấp các tính năng liên quan đến YouTube. Tùy cấu hình của máy chủ, bot có thể liên kết tài khoản Discord với kênh YouTube, theo dõi livestream YouTube và hiển thị sự kiện donate/quà tặng livestream trong Discord.

## 2. Thông tin Jion xử lý

Tùy tính năng được sử dụng, Jion có thể xử lý:

- **Thông tin định danh Discord:** Discord User ID, Server/Guild ID và Discord Channel ID dùng cho cấu hình và gửi thông báo.
- **Thông tin kênh YouTube:** Channel ID, tên kênh công khai, handle và URL ảnh đại diện công khai do YouTube Data API trả về.
- **Thông tin xác minh:** mã xác minh tạm thời dùng một lần, Discord User ID, Server ID, YouTube Channel ID được gửi và thời điểm hết hạn mã.
- **Thông tin livestream và donate:** YouTube Video ID và Channel ID, tên hiển thị công khai, mã sự kiện, số tiền hoặc giá trị quà tặng, loại tiền tệ, bình luận công khai hoặc tên quà tặng và thời gian sự kiện.
- **Cấu hình máy chủ và dữ liệu phục vụ quản trị:** danh sách kênh YouTube được theo dõi, thiết lập kênh thông báo và một số bản ghi giúp quản trị viên kiểm tra trạng thái liên kết của thành viên.
- **Nhật ký vận hành:** sự kiện kỹ thuật và thông tin lỗi cần thiết để vận hành, bảo vệ và xử lý sự cố.

## 3. Message Content Intent và tin nhắn Discord

Jion sử dụng Message Content Intent để phát hiện link kênh YouTube mà thành viên chủ động gửi trong máy chủ có bot. Bot đọc nội dung tin nhắn để nhận diện URL kênh YouTube được hỗ trợ và bắt đầu quy trình xác minh liên kết.

Jion không chủ đích lưu toàn bộ nội dung tin nhắn được gửi để xác minh vào cơ sở dữ liệu. Sau khi bắt đầu quy trình, bot lên lịch xóa tin nhắn gốc sau năm phút. Việc xóa có thể thất bại nếu bot không đủ quyền Discord, tin nhắn không còn tồn tại hoặc bot đang ngoại tuyến.

Jion không cần đọc các cuộc trò chuyện không liên quan để quảng cáo hoặc lập hồ sơ người dùng. Nội dung tin nhắn được sử dụng cho tính năng phát hiện link YouTube.

## 4. Xác minh kênh YouTube

Thành viên có thể gửi URL kênh YouTube hoặc sử dụng lệnh liên kết của bot. Jion tạo mã tạm thời và yêu cầu thành viên thêm mã vào phần mô tả công khai của đúng kênh YouTube. Khi thành viên yêu cầu xác minh, Jion sử dụng YouTube Data API để đọc mô tả công khai của kênh và kiểm tra mã.

Mã được thiết kế để chỉ sử dụng một lần và hết hạn sau khoảng mười phút. Khi xác minh thành công, Jion lưu liên kết giữa Discord User ID và YouTube Channel ID để nhận diện kênh trong những lần sử dụng sau.

Quy trình này **không sử dụng Google OAuth hoặc đăng nhập Google**, không yêu cầu mật khẩu Google và không yêu cầu quyền truy cập tài khoản Google. Quy trình chỉ xác minh khả năng chỉnh sửa phần mô tả của kênh tại thời điểm xác minh, không phải xác minh danh tính pháp lý.

## 5. Theo dõi livestream và sự kiện donate

Khi máy chủ cấu hình các kênh YouTube cần theo dõi, Jion kiểm tra livestream và đọc dữ liệu YouTube Live Chat cần thiết cho các tính năng của bot. Bot kiểm tra tin nhắn chat để tìm mã liên kết được hỗ trợ và xử lý các sự kiện Super Chat, Super Sticker và quà tặng công khai.

Đối với sự kiện donate và quà tặng được hỗ trợ, Jion có thể lưu mã sự kiện, YouTube Channel ID và Video ID, số tiền hiển thị, loại tiền tệ hoặc thông tin quà tặng, bình luận công khai hoặc tên quà tặng, Server ID và thời điểm sự kiện. Bot có thể hiển thị thông tin sự kiện liên quan trong kênh thông báo Discord do máy chủ cấu hình.

Jion không chủ đích lưu trữ toàn bộ tin nhắn YouTube Live Chat như một kho lưu trữ hội thoại.

## 6. Mục đích sử dụng thông tin

Thông tin được sử dụng để:

- Phát hiện link kênh YouTube thành viên gửi cho bot.
- Xác minh quyền kiểm soát kênh YouTube bằng mã tạm thời trong phần mô tả.
- Duy trì liên kết Discord với YouTube.
- Theo dõi livestream do máy chủ lựa chọn.
- Ghi nhận và hiển thị sự kiện donate/quà tặng được hỗ trợ, cũng như cung cấp các tính năng liên quan như thống kê hoặc bảng xếp hạng donate.
- Ngăn bản ghi sự kiện trùng lặp, duy trì cấu hình máy chủ và xử lý lỗi kỹ thuật.

Jion không bán thông tin cá nhân và không sử dụng nội dung tin nhắn để huấn luyện mô hình AI.

## 7. Lưu trữ, máy chủ và dịch vụ bên thứ ba

Jion lưu dữ liệu ứng dụng trong MongoDB. Bot được lưu trữ và chạy trên Render. Bot giao tiếp với Discord để cung cấp tính năng và với YouTube Data API/YouTube để lấy thông tin kênh và livestream.

Các nhà cung cấp này xử lý thông tin theo điều khoản và chính sách quyền riêng tư riêng. Người vận hành Jion không kiểm soát các hoạt động dữ liệu độc lập của họ.

Các chính sách liên quan:
- Chính sách quyền riêng tư Discord: https://discord.com/privacy
- Chính sách quyền riêng tư Google: https://policies.google.com/privacy
- Điều khoản dịch vụ YouTube: https://www.youtube.com/t/terms

## 8. Thời gian lưu trữ và xóa dữ liệu

Bản ghi xác minh tạm thời được cấu hình tự hết hạn sau khoảng mười phút. Tin nhắn Discord gốc được gửi để bot tự động phát hiện link được lên lịch xóa sau năm phút, tùy các giới hạn đã nêu ở trên.

Liên kết Discord–YouTube sau khi xác minh sẽ được lưu cho đến khi người dùng hủy liên kết hoặc dữ liệu được xóa. Người dùng có thể dùng lệnh `/unlink` để xóa liên kết kênh của chính mình. Quản trị viên máy chủ có quyền phù hợp có thể dùng lệnh `/huylienket` để xóa liên kết của một thành viên.

Hủy liên kết chỉ xóa bản ghi liên kết; thao tác này không tự động xóa lịch sử donate, quà tặng, livestream hoặc dữ liệu máy chủ có thể cần cho các tính năng của bot. Để yêu cầu xem xét hoặc xóa các dữ liệu khác, hãy liên hệ người vận hành qua phương thức liên hệ riêng do chủ bot hoặc máy chủ cung cấp.

## 9. Chia sẻ và cung cấp thông tin

Jion không bán thông tin cá nhân. Thông tin có thể được Discord, YouTube/Google, MongoDB và Render xử lý trong phạm vi cần thiết để vận hành dịch vụ. Thông tin sự kiện có thể được đăng trong các kênh Discord do máy chủ cấu hình. Thông tin cũng có thể được cung cấp khi pháp luật yêu cầu hoặc khi cần thiết hợp lý để bảo vệ dịch vụ và người dùng.

## 10. Bảo mật

Người vận hành áp dụng các biện pháp hợp lý nhằm bảo vệ thông tin xác thực của bot và dữ liệu được lưu trữ. Không dịch vụ trực tuyến nào có thể đảm bảo an toàn tuyệt đối. Người dùng không nên gửi mật khẩu, bot token, API key hoặc thông tin xác thực bí mật cho Jion hay đăng chúng trong kênh công khai.

## 11. Quyền riêng tư của trẻ em

Jion là công cụ cộng đồng và không được thiết kế để thu thập thông tin cá nhân nhạy cảm của trẻ em. Chủ máy chủ và người dùng cần tuân thủ yêu cầu độ tuổi của Discord và pháp luật áp dụng. Không gửi thông tin cá nhân nhạy cảm thông qua bot.

## 12. Thay đổi chính sách

Chính sách có thể được cập nhật khi tính năng hoặc cách xử lý dữ liệu của Jion thay đổi. Ngày có hiệu lực ở đầu tài liệu sẽ được cập nhật khi có thay đổi quan trọng.

## 13. Liên hệ và yêu cầu về quyền riêng tư

Nếu có câu hỏi về quyền riêng tư hoặc muốn yêu cầu truy cập, chỉnh sửa hay xóa thông tin, hãy liên hệ người vận hành Jion qua phương thức liên hệ riêng sau:

**[BỔ SUNG EMAIL HOẶC PHƯƠNG THỨC HỖ TRỢ RIÊNG TRƯỚC KHI CÔNG KHAI]**

Không gửi mật khẩu, mã xác minh, bot token, API key hoặc thông tin bí mật khác trong yêu cầu quyền riêng tư.
