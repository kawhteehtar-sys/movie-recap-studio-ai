# Movie Recap Studio – integrated phase

## Included in this phase
- Email/password registration and login
- Free user key / Premium server key selection in backend
- WavePay details and QR remain blank until Admin fills them in
- Payment submission: sender phone number + transaction last 4 digits + screenshot
- Admin payment review and Premium activation/deactivation
- Admin feature toggles
- TTS endpoint, auto-dub endpoint, blur/subtitle MP4 render endpoint
- Per-user project save/load endpoints

## Run
Python 3.10+:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000

Set environment variables from .env.example before starting. Admin account is created at first startup only when ADMIN_PASSWORD is set. Use a strong password and HTTPS. The frontend defaults to http://127.0.0.1:8000 for local testing; set `window.MRS_BACKEND_URL` in index.html to your deployed API URL before hosting.

## WavePay
Admin Dashboard > WavePay Settings can be left blank now. Later Admin can enter WavePay phone, account name, premium price, number of days and QR image. The customer payment form asks for phone, transaction's last four digits and proof screenshot. Payment is manual; no WavePay API verification is claimed.

## Important deployment notes
- Use a persistent disk for DATABASE_PATH and DATA_DIR. Render ephemeral storage can be lost on restart/redeploy.
- Restrict CORS_ORIGINS to the real frontend domain.
- FFmpeg and ffprobe must be installed on the server, and a Myanmar font such as Noto Sans Myanmar must be available for subtitle rendering.
- The Gemini model and Files API availability depend on your Google AI project and account.
- Edge TTS depends on a third-party service and can change without notice.
- This is a coding phase, not a deployed/tested production service. Verify endpoints on your host before using real payments.
- Voice cloning is still OFF/not implemented. Do not collect anyone else's voice without permission.


Temporary media cleanup
-----------------------
The MP4 render endpoint writes the uploaded source video, narration audio, and rendered output into a temporary per-job folder. That folder is removed after the MP4 response has been sent. The browser then keeps the returned MP4 download link for up to 10 minutes; when the timer expires, the page revokes the local download URL and hides the link. The source video and generated MP3 are also released from the editor after a successful export. A file already downloaded to the user's device is not deleted by the website.

Note: browser memory cleanup is best-effort and the 10-minute timer runs only while the page remains open. Closing/reloading the page clears the temporary browser download link.
