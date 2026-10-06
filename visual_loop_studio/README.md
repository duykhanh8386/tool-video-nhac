# Visual Loop Studio

Ứng dụng desktop local cho Windows để tạo visual nhạc 60 giây, kiểm tra kết quả, sau đó loop visual và mix audio thành video dài đúng bằng thời lượng main music.

## Tải EXE portable

Mở trang [GitHub Releases](https://github.com/duykhanh8386/tool-video-nhac/releases/latest) và tải `VisualLoopStudio-Windows-x64.exe`. EXE đã chứa Python, Qt, FFmpeg và FFprobe nên máy khác không cần cài Python hay FFmpeg.

Ứng dụng tự tạo hoặc cập nhật shortcut **Visual Loop Studio** trên Desktop khi chạy bản EXE đóng gói. Shortcut luôn trỏ tới file EXE đang chạy và dùng icon của ứng dụng.

Ứng dụng tự kiểm tra bản mới khi khởi động. Bạn cũng có thể bấm **Check for Updates** ở thanh bên hoặc menu **Help**. File cập nhật được kiểm tra SHA-256 trước khi thay thế EXE và khởi động lại.

## Cài đặt

Yêu cầu:

- Windows 10/11
- Python 3.11+
- FFmpeg và FFprobe có trong `PATH` (hoặc khai báo đường dẫn trong Settings)

Từ thư mục gốc, chạy `install.bat` một lần. Sau đó dùng `run.bat` để mở ứng dụng.

Hoặc chạy thủ công:

```powershell
cd visual_loop_studio
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Ứng dụng dùng `PySide6-Essentials`, OpenCV headless và NumPy; toàn bộ xử lý media production vẫn chạy qua FFmpeg nên không cần Qt WebEngine/Multimedia.

## Workflow

1. Trong **Visual Creator — 60s**, chọn background, thêm text/logo/artwork/waveform, effect stack và color filter.
2. Chọn output folder rồi bấm **Render 1 Minute**. Output luôn là 60 giây và không ghi đè file cũ.
3. Kiểm tra bằng **Play Output**.
4. Sang **Loop Video + Music**, chọn visual vừa render và main music. Background music là tùy chọn.
5. Bấm **Analyze** để xem stream và thời lượng. Bấm **Start Render** để tạo final video.

Main music luôn là master clock. Video và background audio dùng FFmpeg streaming (`-stream_loop -1`), vì vậy không nạp toàn bộ video nhiều giờ vào RAM.

## Chức năng chính

- Hybrid Auto Layout với ba chế độ `AUTO`, `TEMPLATE`, `MANUAL`.
- Phân tích thumbnail background để lập bản đồ độ sáng/chi tiết/saliency; OpenCV tăng cường nhận diện mặt và người.
- Template chỉ là vùng ưu tiên. Engine vẫn tránh important region, kiểm tra va chạm và safe margin theo từng background.
- Mọi element dùng tọa độ chuẩn hóa `x/y/width/height`, cùng `anchor`, `rotation`, `opacity`, `z-order`, `locked`, `visible`.
- Kéo trực tiếp để di chuyển; kéo handle góc phải-dưới để resize; `Ctrl + mouse wheel` để rotate.
- Click không kéo vào Logo, Artwork, Icon nền tảng hoặc Sóng để mở ngay hộp chọn file tương ứng.
- Snap theo canvas center, safe margin, grid hoặc element khác; có alignment guides.
- `AUTO COMPOSE`, `TRY ANOTHER LAYOUT`, `RESET`, lock/hide từng element, duplicate và căn giữa.
- Hỗ trợ tiêu đề, phụ đề, nghệ sĩ, nội dung thêm, playlist nhiều dòng, logo, artwork, biểu tượng nền tảng và file sóng.
- Bộ chọn font hệ thống kiểu Word, cỡ chữ/đậm/nghiêng riêng cho từng loại text; preview và FFmpeg dùng cùng thang cỡ chữ 1080p để tránh chữ bị nhỏ khi render.
- Mỗi element ảnh hỗ trợ `Normal`, `Lighten`, `Screen` và `Linear Dodge (Add)`; click element trên preview để chọn chế độ trong **Bố cục thông minh**.
- Camera nền mặc định đứng yên. Có thể dùng ảnh nguồn + prompt để tạo video image-to-video bằng Veo 3.1; ảnh nguồn được dùng làm cả khung đầu và cuối nhằm ưu tiên vòng lặp liền mạch.
- Tích hợp Wan 2.2, Wan DMD, LTX-Video 2B Distilled và HunyuanVideo 1.5 qua ComfyUI localhost. Workflow mặc định chỉ dùng core node local, từ chối node API/Cloud, nên không tiêu hao token/credit.
- Tích hợp **Google Vids Web — Beta** qua profile Chrome/Edge riêng: người dùng tự đăng nhập một lần trên từng máy, app lần lượt tải ảnh/prompt lên giao diện Vids, tải MP4 về và dùng clip trong pipeline bố cục/render 60 giây. Không lưu mật khẩu Google và không dùng GPU local cho bước tạo clip web.
- Nguồn ảnh AI có ba chế độ: một ảnh, chọn nhiều ảnh riêng lẻ hoặc toàn bộ thư mục. Một prompt dùng chung → mỗi ảnh tạo một clip AI local/Veo → tự ghép cùng text/logo/effect/bố cục → mỗi ảnh xuất một video. GPU local xử lý tuần tự.
- File sóng PNG/GIF/MOV/MP4 chạy lặp theo thời gian riêng, không phản ứng theo âm lượng nhạc; có tùy chọn xóa nền trắng.
- Input **Overlay toàn cảnh** nhận PNG/GIF/MOV/MP4, crop phủ kín khung, phát/lặp đúng thời lượng file và có blend mode cùng độ mờ riêng. Preview giải mã frame video thật thay vì giữ frame đầu.
- Các hiệu ứng toàn khung tích hợp hiển thị chuyển động/cường độ ngay trong preview; có thể chồng thêm overlay MOV bên ngoài để dùng hiệu ứng dựng sẵn như trong CapCut.
- Preview nhẹ 16:9 cho image/video background, artwork, logo, text, waveform, color preset và nhiều full-frame effect.
- Render visual đúng 60 giây ở 1080p/4K, 24–60 fps.
- Nút render toàn bộ dùng lại ảnh đã chọn ở **Nguồn ảnh AI** hoặc các clip AI vừa tạo, không yêu cầu chọn lại một thư mục background riêng. Danh sách clip AI được lưu trong project để có thể render lại với bố cục hiện tại.
- Effect stack có add/remove/reorder/enable/intensity; mặc định rỗng.
- Color filter và `.cube` LUT; mặc định `NONE`.
- Dò media bằng FFprobe theo stream thực, không chỉ dựa vào đuôi file.
- Auto NVENC khi GPU hoạt động; fallback libx264.
- Progress, tốc độ, FPS, ETA, dung lượng output; cancel không khóa GUI.
- Mọi tiến trình FFmpeg/FFprobe/GPU/updater chạy ẩn, không làm cửa sổ CMD nhấp nháy; output được thu vào giao diện và log render.
- Audio Mixer sáu track; thời lượng theo main audio.
- Batch queue hỗ trợ 1–3 job đồng thời, mặc định 1 để ổn định NVENC/RAM.
- Màn hình **AI Video hàng loạt** cung cấp một hàng đợi chung cho Seedance qua BytePlus LAS API chính thức, Veo qua Gemini API và ComfyUI local. Có thể nạp prompt TXT/CSV, chọn ảnh/thư mục, đặt số lượng, theo dõi provider job ID, khôi phục polling sau khi mở lại ứng dụng, lọc job và xuất báo cáo.
- Gemini và BytePlus API key được lưu bằng Windows Credential Manager (hoặc đọc từ `GEMINI_API_KEY` / `BYTEPLUS_LAS_API_KEY`), không còn ghi secret mới vào `settings.json`. Retry chỉ áp dụng cho lỗi kỹ thuật; lỗi quota, xác thực và policy không bị gửi lặp.
- Có giới hạn ngân sách ngày/batch dựa trên đơn giá cloud/giây do người dùng cấu hình. Đây là ước tính trước khi gửi; hóa đơn thực tế vẫn theo nhà cung cấp.
- Lưu/mở project `.vls.json`; nhớ settings và output folder.
- Log render trong `visual_loop_studio/logs/`.
- Gemini/Veo API và ComfyUI đều là tích hợp tùy chọn; chức năng render lõi không cần AI. Nhập Gemini API key trong Cài đặt hoặc biến môi trường `GEMINI_API_KEY` để gọi Veo.

## AI Video local

1. Trong Visual Creator chọn **AI Video Local**, rồi chọn Wan Chất lượng, Wan DMD, LTX-Video 2B Distilled hoặc HunyuanVideo 1.5 và bấm nút cài/tải model đang chọn.
2. Chọn ổ lưu. Tool tự tính dung lượng còn thiếu, nhận diện NVIDIA CUDA, Intel Arc XPU, AMD hoặc CPU rồi tải đúng ComfyUI Portable và đúng bộ model; tải dở có thể tiếp tục.
3. Sau khi cài, ComfyUI tự chạy ẩn cùng Visual Loop Studio và tự tắt khi thoát. Có thể bấm **Kiểm tra ComfyUI + model** để xác nhận.
4. Wan Chất lượng dùng 20 bước/CFG 5. Wan DMD dùng 4 bước/CFG 1. LTX và Hunyuan Step Distilled dùng 8 bước/CFG 1. Hunyuan được khóa 832×480 và chỉ cho NVIDIA CUDA, khuyến nghị từ 16 GB VRAM.
5. Để bulk, chọn nhiều ảnh hoặc cả thư mục ở **Nguồn ảnh AI**, nhập prompt chung và bấm **Tạo AI local + render toàn bộ ảnh**. Tool tạo đủ clip AI trước, sau đó tự render từng clip thành visual 60 giây bằng cùng snapshot bố cục.

Wan 2.2 Chất lượng và LoRA DMD 4 bước đều dùng Apache 2.0. LTX dùng Open Weights License 0.X (thương mại miễn phí dưới ngưỡng doanh thu trong giấy phép; nội dung công khai phải đánh dấu AI). Hunyuan dùng Tencent Hunyuan Community License với giới hạn lãnh thổ/phân phối và yêu cầu đánh dấu AI. Tất cả chạy trên máy nên không có phí token/credit; chi phí thực tế là điện, thời gian GPU và dung lượng đĩa. Không bật Comfy Cloud/Partner/API node nếu muốn bảo đảm chạy local hoàn toàn.

## Google Vids Web — Beta

1. Máy cần có Google Chrome hoặc Microsoft Edge. Chọn engine Google Vids Web và bấm nút đăng nhập lần đầu.
2. Người dùng tự đăng nhập trong cửa sổ browser riêng và đóng browser khi đã vào được Vids. Profile được lưu cục bộ trong thư mục dữ liệu Visual Loop Studio; không dùng chung tự động giữa các máy.
3. Ảnh đơn tạo một MP4 và tự chọn làm background. Danh sách/thư mục ảnh chạy tuần tự với prompt chung, lưu trong `Google_Vids_Clips`, sau đó tự render đủ visual 60 giây với snapshot bố cục hiện tại.
4. Có thể hủy hàng đợi; các clip đã tải xong vẫn được giữ để dùng lại. Ảnh lỗi không làm mất các clip đã hoàn tất.

Google Vids hiện không có API công khai dành cho việc tạo clip. Adapter này dùng tự động hóa giao diện web bằng Playwright, vì vậy phụ thuộc giao diện/quota/chính sách tài khoản của Google và có thể cần cập nhật selector khi Google đổi UI. CAPTCHA hoặc xác minh bảo mật phải do người dùng xử lý trong browser. Bản EXE chỉ đóng gói Playwright, còn Chrome/Edge dùng bản đã cài trên máy.

## Muse AI batch với ba Chrome profile độc lập

Trang **Muse Batch — 3 tài khoản** có ba tab tương ứng ba `user-data-dir` cố định: `data/muse_profiles/account_1`, `account_2`, `account_3`. Chọn thư mục ảnh, prompt chung và output; ảnh được chuẩn hóa, loại trùng, sắp xếp rồi chia round-robin cho ba worker. Mỗi worker sở hữu riêng Selenium driver, hàng đợi, worker thread, asyncio task, stop event và lock.

Selenium chỉ tương tác trên `muse.ai`, `auth.muse.ai`, `accounts.google.com` và trang xác nhận tài khoản chính chủ `myaccount.google.com`, dùng selector theo `data-testid`, role, aria-label hoặc placeholder. Mỗi profile đi qua `accounts.google.com/AccountChooser` với email của đúng tab để ưu tiên tái sử dụng phiên Google đã được đăng nhập bằng Chrome thường, thay vì ép `AddSession` lại mỗi lần. Nếu chưa có phiên, tool click trực tiếp `identifierNext`/`passwordNext`. Sau khi Google chuyển về YouTube, tool dùng Windows UI Automation để bấm nút Chrome `Continue as …` trong đúng cửa sổ được đánh dấu riêng; nếu hồ sơ đã được xác nhận và nút không còn xuất hiện thì bước này được bỏ qua. Tool tiếp tục xác minh email trên Google trước khi mở Muse. Trang `ManageAccount` hoặc `myaccount.google.com` mà không thấy đúng email không được coi là đăng nhập thành công. Mật khẩu chỉ được phép điền khi hostname chính xác là `accounts.google.com`; thông tin trong ba tab chỉ được giữ tạm trong bộ nhớ và không được ghi vào settings/checkpoint/log. Nếu Google báo “browser or app may not be secure”, session dừng ở `LOGIN_REQUIRED`; tool không vượt cơ chế chống tự động hóa. CAPTCHA, 2FA, passkey và xác minh thiết bị vẫn do người dùng xử lý trực tiếp trong Chrome. Profile từng bị luồng cũ đưa nhầm vào `/access` được xóa cookie/localStorage của riêng `muse.ai` và thử lại đúng một lần; dữ liệu đăng nhập Google không bị xóa. Nếu Muse vẫn báo waitlist hoặc chưa hỗ trợ khu vực, session được đánh dấu lỗi riêng; tool không vượt giới hạn cấp quyền này.

Manager ghi checkpoint không bí mật sau mỗi trạng thái job. Job đã submit không được gửi lại khi tiếp tục/restart; lỗi download chỉ retry download. Nút dừng chỉ tác động đúng worker, còn **Dừng tất cả Muse** không gọi tới Auto Registry hoặc YouTube. Prompt, phân bổ, tiến độ và kết quả vẫn giữ nguyên sau khi dừng; driver được quit khi ứng dụng đóng nhưng thư mục profile không bị xóa.

Mỗi tab có ô email và mật khẩu Google nhưng không có nút đăng nhập riêng. Khi bấm **Bắt đầu cả 3**, manager mở đồng thời ba Chrome profile, đăng nhập Google rồi mới mở Muse. Tài khoản nào đạt `READY` sẽ bắt đầu hàng đợi ảnh của chính nó ngay; một tài khoản còn đăng nhập, ở waitlist hoặc lỗi không chặn hai tài khoản còn lại. Prompt/cài đặt/output được checkpoint trước khi mở Chrome. Ô mật khẩu dùng chế độ che ký tự và bị xóa ngay khi bắt đầu; nếu profile còn phiên thì có thể để trống.

Luồng composer hiện tại của Muse dùng input file, preview ảnh dạng `blob:`, ô `Message` và nút `Send`. Mỗi worker gửi đúng một ảnh với một prompt, chờ đúng video mới nằm sau prompt đó, tải xong rồi mới chuyển sang ảnh kế tiếp; các worker của những tài khoản khác nhau vẫn chạy đồng thời. Tool chỉ tải `currentSrc/src` của chính thẻ `<video>` đã ghép với prompt, không bấm nút Download chung của card nên không thể tải nhầm ảnh đính kèm. Trước từng lượt, worker chuyển về đúng handle Muse đã chọn trong Chrome profile riêng; checkpoint giữ fingerprint để lỗi download chỉ tải lại đúng video và không gửi lại prompt.

Provider **Muse AI Web** trong **AI Video hàng loạt** vẫn chạy với đúng tài khoản được chọn và dừng phần Muse khi phát hiện hết quota; không có tự động đổi tài khoản.

## AI Video hàng loạt — Seedance, Veo và ComfyUI

1. Mở **Cài đặt**, nhập Gemini API key và/hoặc BytePlus LAS API key chính thức. Với cloud, điền đơn giá ước tính cùng giới hạn ngân sách ngày/batch nếu muốn app chặn chi phí trước khi gửi.
2. Mở **AI Video hàng loạt**, chọn provider/model. Duration, tỉ lệ, resolution, audio và giới hạn ảnh được lấy từ capability của model.
3. Nhập prompt trực tiếp hoặc nạp TXT/CSV; có thể chọn nhiều ảnh hoặc quét cả thư mục. App hiển thị trước tổng số job và chi phí ước tính.
4. Xác nhận quyền sử dụng tài sản rồi tạo batch. Job được lưu tại `data/ai_jobs.json`; khi app mở lại, cloud job có provider ID sẽ tiếp tục polling thay vì gửi trùng.
5. Có thể lọc theo trạng thái/provider/model/batch/thời gian, xem chi tiết, hủy, retry lỗi kỹ thuật, mở video/thư mục và xuất CSV/JSON. Audit đã che secret nằm tại `data/ai_audit.jsonl`.

Seedance dùng endpoint tác vụ bất đồng bộ của BytePlus LAS, tải kết quả qua file `.part` rồi đổi tên khi hoàn tất. Phân hệ này không có kho tài khoản, cookie/session import, proxy rotation hoặc cơ chế né quota.

## Kiểm thử

```powershell
cd visual_loop_studio
python -m unittest discover -s tests -v
```

Các test bao phủ FFprobe parser, master duration, loop command, effect defaults/order/seam, và project round-trip.

## Build và phát hành tự động

Workflow `.github/workflows/build-release.yml` chạy trên Windows sau mỗi push vào nhánh `Dola-AI`:

1. Cài dependency và chạy test.
2. Đóng gói một EXE portable bằng PyInstaller.
3. Nhúng FFmpeg và FFprobe vào EXE.
4. Tạo file SHA-256.
5. Tạo version mới dạng `1.0.<run>.<attempt>`.
6. Đăng EXE lên GitHub Releases và đánh dấu là bản mới nhất.

Updater chỉ chọn release có tag `dola-ai-v*` và chỉ đề nghị cập nhật khi commit đóng dấu của release khác commit đang chạy. Các release thuộc `main` hoặc nhánh khác không được nút cập nhật sử dụng.

Để build thủ công trên Windows, chạy `build_windows.ps1` từ thư mục gốc.

## Ghi chú

- Các hiệu ứng hạt phức tạp được preview theo kiểu procedural nhẹ; renderer FFmpeg dùng các xấp xỉ streamable để giữ RAM ổn định.
- Background crossfade hiện dùng fade mềm ở đầu stream loop. Không tạo file audio lặp thủ công.
- Nếu encoder GPU báo lỗi, chọn `Auto` hoặc `libx264`.
