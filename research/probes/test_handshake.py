import usb.core, usb.util, usb.backend.libusb1 as b, time, sys, threading, os
be=b.get_backend(find_library=lambda x:'/opt/homebrew/lib/libusb-1.0.dylib')
t0=time.monotonic(); T=lambda: f"{time.monotonic()-t0:6.2f}"
dev=None
while dev is None:
    dev=usb.core.find(idVendor=0x2dc8,idProduct=0x2027,backend=be)
    if dev is None:
        if time.monotonic()-t0>60: sys.exit("controller not found")
        time.sleep(0.1)
time.sleep(1.0)  # let macOS finish configuring it
usb.util.claim_interface(dev,0); print(T(),"claimed"); sys.stdout.flush()
n=[0]
def reader():
    last=None
    while True:
        try: x=bytes(dev.read(0x81,64,timeout=0))   # 0 = wait forever, never aborts the pipe
        except usb.core.USBError as e: print(T(),"read error:",e); sys.stdout.flush(); return
        n[0]+=1
        if x!=last and n[0]<60: print(T(),"<-",x.hex(" ")); sys.stdout.flush()
        last=x
threading.Thread(target=reader,daemon=True).start()
time.sleep(0.05)
usb.util.claim_interface(dev,1); dev.set_interface_altsetting(interface=1, alternate_setting=0); print(T(),"claimed intf1, set alt 0 via API")
for s,p in enumerate([[5,0x20,0,1,0],[0x0a,0x20,0,3,0,1,0x14],[6,0x20,0,2,1,0]]):
    p[2]=s; dev.write(0x02,bytes(p),timeout=1000); print(T(),"->",bytes(p).hex(" "))
sys.stdout.flush()
time.sleep(20)
print("packets:",n[0]); sys.stdout.flush()
os._exit(0)
