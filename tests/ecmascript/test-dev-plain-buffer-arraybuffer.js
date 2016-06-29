/*
 *  Plain buffer values behave like ArrayBuffers for Ecmascript code in
 *  Duktape 2.x.
 */

/*---
{
    "custom": true
}
---*/

function createPlain() {
    var pb = Duktape.Buffer(16);
    for (var i = 0; i < 16; i++) {
        pb[i] = 0x61 + i;
    }
    return pb;
}

function createArrayBuffer() {
    var ab = new ArrayBuffer(16);
    for (var i = 0; i < 16; i++) {
        ab[i] = 0x61 + i;
    }
    return ab;
}

/*===
basic test
object
object
[object ArrayBuffer]
[object ArrayBuffer]
[object ArrayBuffer]
[object ArrayBuffer]
true
true
===*/

function basicTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();

    // typeof
    print(typeof pb);  // 'buffer' in Duktape 1.x, 'object' in Duktape 2.x
    print(typeof ab);     // 'object'

    // class name in Object.prototype.toString()
    print(Object.prototype.toString.call(pb));  // '[object Buffer]' in Duktape 1.x, '[object ArrayBuffer]' in Duktape 2.x
    print(Object.prototype.toString.call(ab));  // '[object ArrayBuffer]'

    // instanceof
    print(pb instanceof ArrayBuffer);
    print(ab instanceof ArrayBuffer);
}

function propertyTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();

    // ArrayBuffer virtual properties
    print(pb.length);
    print(ab.length);
    print(pb.byteLength);
    print(ab.byteLength);
    print(pb.byteOffset);
    print(ab.byteOffset);
    print(pb.BYTES_PER_ELEMENT);
    print(ab.BYTES_PER_ELEMENT);
    print(pb.buffer);  // not present
    print(ab.buffer);  // not present
    print(pb[0]);
    print(ab[0]);
}

function readWriteCoercionTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();
}

function operatorTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();

    // '+' operator
    print(pb + pb);  // 'abcdefghijklmnopabcdefghijklmnop' in Duktape 1.x, '[object ArrayBuffer][object ArrayBuffer]' in Duktape 2.x
    print(ab + ab);  // '[object ArrayBuffer][object ArrayBuffer]'

    // equality comparison: no content comparison in Duktape 2.x when
    // comparing plain buffers using '=='
    print(createPlain() == createPlain());
    print(createPlain() === createPlain());
    print(pb == pb);
    print(pb === pb);
    print(createArrayBuffer() == createArrayBuffer());
    print(createArrayBuffer() === createArrayBuffer());
    print(ab == ab);
    print(ab === ab);

    // FIXME: compare buffer to number -""-
    // FIXME: compare buffer to string -""-
    // FIXME: compare buffer to object (and vice versa)

    [ 'length', 'byteLength', 'byteOffset', 'BYTES_PER_ELEMENT', -1, 0, 15, 16, '15', '16', '15.0' ].forEach(function (v) {
        print(typeof v, v, v in pb, v in ab);
    });
}

function coercionTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();

    // ES5 coercions

    // ToObject() coercion returns the plain buffer as is, otherwise it would
    // behave differently from an ArrayBuffer which is returned as is.
    print(Object(pb) === pb);
    print(Object(ab) === ab);

    // ToString() coercion
    print(String(pb));
    print(String(ab));

    // ToString goes through ArrayBuffer.prototype
    ArrayBuffer.prototype.toString = function () { return '[Overridden]'; };
    print(String(pb));
    print(String(ab));
    delete ArrayBuffer.prototype.toString;

    // ToString() when overridden .toString() and .valueOf() also return a
    // plain buffer; causes a TypeError (matches V8 behavior for ArrayBuffer)
    ArrayBuffer.prototype.toString = function () { return createPlain(); };
    ArrayBuffer.prototype.valueOf = function () { return createPlain(); };
    try {
        print(String(pb));
    } catch (e) {
        print(e);
    }
    try {
        print(String(ab));
    } catch (e) {
        print(e);
    }
    delete ArrayBuffer.prototype.toString;
    delete ArrayBuffer.prototype.valueOf;

    // Same behavior if .toString() returns an ArrayBuffer object
    ArrayBuffer.prototype.toString = function () { return createArrayBuffer(); };
    ArrayBuffer.prototype.valueOf = function () { return createArrayBuffer(); };
    try {
        print(String(pb));
    } catch (e) {
        print(e);
    }
    try {
        print(String(ab));
    } catch (e) {
        print(e);
    }
    delete ArrayBuffer.prototype.toString;
    delete ArrayBuffer.prototype.valueOf;

    // ToNumber() coerces via ToString(); usually results in NaN but by
    // overriding .toString() one can get a number result
    print(Number(pb));
    print(Number(ab));
    try {
        ArrayBuffer.prototype.toString = function () { return '123'; };
        print(Number(pb));
        print(Number(ab));
    } catch (e) {
        print(e);
    }
    delete ArrayBuffer.prototype.toString;

}

function jsonTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();

    // JSON, JX, and JC
    print(JSON.stringify(pb));  // undefined because type not supported by JSON
    print(JSON.stringify(ab));     // same
    print(Duktape.enc('jx', pb));
    print(Duktape.enc('jx', ab));
    print(Duktape.enc('jc', pb));
    print(Duktape.enc('jc', ab));
}

function viewTest() {
    var pb = createPlain();
    var ab = createArrayBuffer();
    var view;

    // create typedarray on top of plain buffer / ArrayBuffer
    view = new Uint32Array(pb);
    print(Object.prototype.toString.call(view));
    print(view[0]);
    print(Duktape.enc('jx', view));
    view = new Uint32Array(ab);
    print(Object.prototype.toString.call(view));
    print(view[0]);
    print(Duktape.enc('jx', view));
}

try {
    print('basic test');
    basicTest();

    print('property test');
    propertyTest();

    print('read/write coercion test');
    readWriteCoercionTest();

    print('operator test');
    operatorTest();

    print('coercion test');
    coercionTest();

    print('json test');
    jsonTest();

    print('view test');
    viewTest();

    // misc
    // FIXME: regexp exec input; buffer treated like '[object ArrayBuffer]'
} catch (e) {
    print(e.stack || e);
}

// FIXME: ToPrimitive(), ToObject() etc coercions, go through E5 spec

// FIXME: .slice()

// FIXME: instanceof
