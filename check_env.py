import os
import requests
from dotenv import load_dotenv
import json

load_dotenv()
# Get the key from your .env (without the word Bearer)
api_key = os.getenv("DOME_API_KEY")

url = "https://api.domeapi.io/v1/polymarket/markets?market_slug=will-gavin-newsom-win-the-2028-us-presidential-election"
headers = {"Authorization": f"Bearer bf503cfab26f1595e306353ff038a01cc0efb42b"}
 
response = requests.get(url, headers=headers)
data = response.json()
pretty_data = json.dumps(data, indent=4)
print(pretty_data)