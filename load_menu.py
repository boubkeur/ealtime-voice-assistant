import requests
import json

def load_menu_from_all_endpoint():  
    """ not very clean, needs more verification for duplicates check"""
    url = "http://foh-api.modernops.ai/api/menu/all"
    headers = {"Content-Type": "application/json"}

    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()          
    data = r.json()  
    categories = {i['id']: i['name'] for i in data['data']['categories']}

    modifier_groups = {i['id']: { 
                                    'name' : i['name'], 
                                    'minRequired' : i['minRequired'], 
                                    'maxAllowed' : i['maxAllowed'], 
                                    'modifiers' : {modifier["id"]: {
                                                    "name": modifier["name"], 
                                                    "price": modifier["price"]
                                                    } 
                                                   for modifier in i['modifiers']['elements'] if modifier['available']}
                                } 
                        for i in data['data']['modifierGroups']}
    
    items = {i['id']: { 
                        'name' : i['name'] ,
                        'description' : i['description'] ,
                        'price' : i['price'] , 
                        'categoryIds': [categories[i] for  i in i['categoryIds']],
                        'modifier_groups_ids' : [i['id'] for i in i['modifierGroups']]
                      } 
            for i in data['data']['items']}

    live_menu = {'categories': categories, 'items': items , 'modifier_groups': modifier_groups }

    return live_menu

#print(json.dumps(load_menu_from_all_endpoint(), ensure_ascii=False))
