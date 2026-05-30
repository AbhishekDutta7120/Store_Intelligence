@echo off
REM run.bat — Run detection pipeline on all CAM videos
REM Usage: pipeline\run.bat [optional: path to videos folder]
REM Output: data\events.jsonl

SET VIDEO_DIR=%1
IF "%VIDEO_DIR%"=="" SET VIDEO_DIR=.

SET API_URL=http://localhost:8000/events/ingest

echo [INFO] Store Intelligence Detection Pipeline
echo [INFO] Looking for videos in: %VIDEO_DIR%
echo [INFO] API URL: %API_URL%
echo.

REM Collect all mp4 files
SET VIDEOS=
FOR %%f IN ("%VIDEO_DIR%\*.mp4") DO (
    SET VIDEOS=!VIDEOS! "%%f"
)

IF "%VIDEOS%"=="" (
    echo [ERROR] No .mp4 files found in %VIDEO_DIR%
    echo [ERROR] Usage: pipeline\run.bat C:\path\to\videos
    exit /b 1
)

echo [INFO] Found videos: %VIDEOS%
echo [INFO] Starting detection (this will take ~20-40 minutes on CPU)...
echo.

python -m pipeline.detect ^
    --videos %VIDEO_DIR%\CAM_1.mp4 %VIDEO_DIR%\CAM_2.mp4 %VIDEO_DIR%\CAM_3.mp4 %VIDEO_DIR%\CAM_4.mp4 %VIDEO_DIR%\CAM_5.mp4 ^
    --layout config\store_layout.json ^
    --output data\events.jsonl ^
    --api-url %API_URL%

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo [DONE] Detection complete. Events saved to data\events.jsonl
    echo [INFO] Check the dashboard at http://localhost:8000
) ELSE (
    echo.
    echo [ERROR] Detection failed. Check output above.
)
