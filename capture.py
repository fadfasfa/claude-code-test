import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--window-size=1280,1024")

driver = webdriver.Chrome(options=chrome_options)
driver.get("http://127.0.0.1:8000/detail.html?hero=%E4%BA%9A%E7%B4%A2&en=Yasuo&id=157")
time.sleep(4)
driver.save_screenshot(r"c:\Users\apple\claudecode\.ai_workflow\frontend_artifact\ui_screenshot.png")
driver.quit()
print('Screenshot saved successfully')
