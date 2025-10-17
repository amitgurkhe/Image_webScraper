from PIL import Image
import os
f = r'c:\Users\Amit\Notebooks\Allianz_work\RIS\selenium_code\lens_out\debug_upload_failed.png'
if not os.path.exists(f):
    print('PNG_MISSING')
else:
    img = Image.open(f)
    print('PNG_FOUND|%d|%d|%d' % (img.width, img.height, os.path.getsize(f)))
    img.close()
