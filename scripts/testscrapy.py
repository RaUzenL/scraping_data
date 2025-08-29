from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options

opts = Options()
opts.add_argument("--headless=new")

driver = webdriver.Chrome(options=opts)
driver.get("https://www.tripadvisor.com/")
print("Title:", driver.title)

# confirm network capture works
print("Requests captured:", len(driver.requests))
driver.quit()