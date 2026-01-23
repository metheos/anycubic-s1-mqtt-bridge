How to snoop on the official slicer's web views

$env:WEBVIEW2_DEVELOPER_TOOLS="true"
$env:WEBVIEW2_USER_DATA_FOLDER="C:\Users\Metheos\AppData\Local\Temp\WebView2Debug"
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9222"
Start-Process "C:\Program Files\AnycubicSlicerNext\AnycubicSlicerNext.exe"

Open edge and go to edge://inspect/#devices
