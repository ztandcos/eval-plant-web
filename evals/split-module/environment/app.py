def greet(name):
    cleaned = str(name).strip().lower()
    return 'hi:' + cleaned

if __name__ == '__main__':
    print(greet(' Ada '))
