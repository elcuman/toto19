import requests

API_KEY = "3c58da1cd01e402f92825db5752ca930"

print("KEY VAR MI :", bool(API_KEY))
print("KEY UZUNLUK:", len(API_KEY))
print("KEY ILK 4  :", API_KEY[:4] if API_KEY else "YOK")
print("KEY SON 4  :", API_KEY[-4:] if API_KEY else "YOK")

url = "https://v3.football.api-sports.io/status"

headers = {
    "x-apisports-key": API_KEY.strip()
}

print("\nİstek gönderiliyor...")

response = requests.get(
    url,
    headers=headers,
    timeout=15
)

print("\nHTTP STATUS:", response.status_code)
print("RESPONSE:")
print(response.text)