If you're here, you're encountering something like this:

```
lxml.etree.XMLSyntaxError: AttValue length too long, line 2, column 1000000xxx
lxml.etree.XMLSyntaxError: Resource limit exceeded: Buffer size limit exceeded, try XML_PARSE_HUGE, line xxxxxx, column 99yyyyyyy
```

First off, congratulations! That means you probably have at least one text message exceeding **1 billion characters /
~750 MB in size** (base64 encoded), which is ["truly enormous"](https://bugs.launchpad.net/lxml/+bug/2101805)
and [breaks most (all?) XML parsers generally available](https://github.com/ragibson/SMS-MMS-deduplication/issues/8).

Unfortunately, that also means you'll have to make a custom build of `libxml2` and `lxml` and patch in higher limits
yourself, which will
also [disable a safeguard intended to avoid integer overflow](https://gitlab.gnome.org/GNOME/libxml2/-/issues/874).

> [!WARNING]
> This is not without risk! **Do not proceed unless you are reasonably comfortable building from source and/or
> recovering from a broken system.**
>
> **`libxml2` is a dependency of >100 packages in my distro.**
>
> If you accidentally install a bad custom build over the system installation or otherwise remove the system `libxml2`
> without replacement, **you could plausibly brick your machine!**

Anyway, on my system, the custom build can be completed with the following process.

Again, to be crystal clear here,

* **Do not execute these commands without understanding them!**
* Read and run them step by step, resolving any issues that arise along the way
* Consider running this in a VM or creating a Timeshift snapshot before proceeding

```bash
# install various non-universal build dependencies needed by these libraries
# you might need a few more if you haven't built libraries from source before
sudo apt install libtool libxml2-dev libxslt-dev python3-dev cython3
python3 -m pip install setuptools Cython

# download libxml2 source (here, v2.13.6, which is newer than in apt for me)
git clone https://gitlab.gnome.org/GNOME/libxml2 -b v2.13.6
cd libxml2

# change hard limit for XML element size to, e.g., 2 billion
sed -i 's/#define XML_MAX_HUGE_LENGTH 1000000000/#define XML_MAX_HUGE_LENGTH 2000000000/' include/libxml/parserInternals.h

# configure and install libxml2
CFLAGS='-O2 -fno-semantic-interposition' ./configure --prefix=$HOME/libxml2
make check # make sure the tests still pass on your machine
make install
cd ..

# check and see if lxml can already use the new version of libxml2
# if so, you can stop here (but this was not the case on my machine)
LD_LIBRARY_PATH="$HOME/libxml2/lib" python3 -c "
import sys
from lxml import etree

print('%-20s: %s' % ('Python', sys.version_info))
print('%-20s: %s' % ('lxml.etree', etree.LXML_VERSION))
print('%-20s: %s' % ('libxml used', etree.LIBXML_VERSION))
print('%-20s: %s' % ('libxml compiled', etree.LIBXML_COMPILED_VERSION))
print('%-20s: %s' % ('libxslt used', etree.LIBXSLT_VERSION))
print('%-20s: %s' % ('libxslt compiled', etree.LIBXSLT_COMPILED_VERSION))
"

# download lxml source (here, v5.3.1), we need a source release since lxml doesn't include
# we need a source release for the included Cython-generated files
wget https://github.com/lxml/lxml/releases/download/lxml-5.3.1/lxml-5.3.1.tar.gz
tar -xvf lxml-5.3.1.tar.gz
cd lxml-5.3.1

# set some environment variables to get lxml's build to use our user libxml2 installation
export CFLAGS="-I$HOME/libxml2/include"
export LDFLAGS="-L$HOME/libxml2/lib"
export LD_LIBRARY_PATH="-L$HOME/libxml2/lib"
export PKG_CONFIG_PATH="$HOME/libxml2/lib/pkgconfig"

# configure and install lxml2
python3 -m pip install -r requirements.txt
make inplace
python3 test.py # again, make sure the test suite passes
# I actually seem to get a few gzip/http test failures, but they don't matter for my use case
python3 setup.py bdist_wheel
python3 -m pip install dist/lxml-5.3.1-cp312-cp312-linux_x86_64.whl

# now, lxml should definitely be using the new libxml version you cloned, but
# we'll need to explicitly enforce that when executing python
LD_LIBRARY_PATH="$HOME/libxml2/lib" python3 -c "
import sys
from lxml import etree

print('%-20s: %s' % ('Python', sys.version_info))
print('%-20s: %s' % ('lxml.etree', etree.LXML_VERSION))
print('%-20s: %s' % ('libxml used', etree.LIBXML_VERSION))
print('%-20s: %s' % ('libxml compiled', etree.LIBXML_COMPILED_VERSION))
print('%-20s: %s' % ('libxslt used', etree.LIBXSLT_VERSION))
print('%-20s: %s' % ('libxslt compiled', etree.LIBXSLT_COMPILED_VERSION))
"
```

Similarly, you'll need to invoke `dedupe_texts.py` with that library path.

```bash
LD_LIBRARY_PATH="$HOME/libxml2/lib" python3 dedupe_texts.py ...
```