# Google OAuth2 cho xác minh kênh YouTube

## Render

Giữ Web Service hiện tại. Không tạo dịch vụ mới.

Thêm các Environment Variables trong Render:

- `MESSAGE_CONTENT_INTENT=1`
- `GOOGLE_CLIENT_ID` = OAuth Client ID từ Google Cloud
- `GOOGLE_CLIENT_SECRET` = OAuth Client Secret
- `GOOGLE_REDIRECT_URI=https://web-test-2pl0.onrender.com/youtube/callback`

Không commit Client Secret vào GitHub.

## Discord Developer Portal

Trong ứng dụng bot, mở Bot > Privileged Gateway Intents và bật **Message Content Intent**. Nếu không bật, bot không đọc được link người dùng gửi trong tin nhắn thường.

## Google Cloud

1. Chọn hoặc tạo Google Cloud project.
2. Enable **YouTube Data API v3**.
3. Configure OAuth consent screen. Nếu ứng dụng ở chế độ Testing, thêm các Google account thử nghiệm vào Test users.
4. Tạo OAuth Client ID loại **Web application**.
5. Thêm Authorized redirect URI chính xác:

   `https://web-test-2pl0.onrender.com/youtube/callback`

6. Đặt Client ID và Client Secret vào Environment Variables của Render.

Scope được yêu cầu:

`https://www.googleapis.com/auth/youtube.readonly`

Đây là quyền đọc YouTube cần thiết để xác định kênh của tài khoản Google đã cấp quyền.

## Luồng sử dụng

1. Người dùng gửi URL kênh dạng `youtube.com/@handle` hoặc `youtube.com/channel/UC...` trong server.
2. Bot phản hồi bằng nút xác minh Google.
3. Người dùng đăng nhập Google và cấp quyền.
4. Callback gọi YouTube Data API `channels.list?mine=true` và so sánh Channel ID với URL đã gửi.
5. Chỉ khi trùng ID, bot mới lưu liên kết vào collection `links`.
6. Nếu kênh đã thuộc Discord ID khác, bot từ chối và giữ liên kết cũ.
7. Tin nhắn gốc chứa link được lên lịch xóa sau 5 phút.

OAuth state và yêu cầu chờ xác minh được lưu trong MongoDB với thời hạn. Access token chỉ dùng trong callback và không được lưu.

## Giới hạn cần biết

- Message Content Intent phải được bật cả trong Discord Developer Portal và biến Render.
- Google OAuth app ở chế độ Testing chỉ cho phép các tài khoản nằm trong Test users; triển khai công khai có thể cần hoàn tất quy trình xác minh OAuth của Google.
- Bot cần quyền Manage Messages để xóa tin nhắn gốc. Nếu thiếu quyền, xác minh vẫn có thể chạy nhưng việc xóa tin nhắn sẽ thất bại.
- Render Free có thể spin down khi không hoạt động, khiến callback đầu tiên bị chậm.
- Hiện bộ nhận diện link tự động hỗ trợ URL dạng `/channel/UC...` và `/@handle`. URL dạng custom `/c/...` hoặc `/user/...` chưa được tự động nhận diện.
